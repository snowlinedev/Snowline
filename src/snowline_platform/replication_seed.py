"""The seed procedure behind `snowline replicate seed` (replication-continuity
§7, issue #82).

The bus is a delta fabric, not an event-sourced log (§7): outbox rows are pending
deliveries and history is not retained for replay, so a spoke is stood up from a
Postgres snapshot of the primary — and THE ORDER IS LOAD-BEARING. Getting it
wrong is silent data loss, which is why §7 reads as a literal checklist and this
module implements it in exactly that order:

  1. PRIME the primary→spoke stream FIRST (before the dump). The seed creates the
     PRIMARY's outbound subscription — the one exception to §5's receiver-mints
     rule: the spoke's store does not exist yet, so the script plays the
     receiver's part, MINTS the secret, carries it (never logged, dropped after
     step 3), and injects it into the spoke in step 3. From this instant every
     primary write emits into the stream — so a write landing between the dump
     and a later-created subscription can be in neither the snapshot nor a
     delivery. Priming first closes that gap.
  2. DUMP each opted-in store (and the platform DB for scopes) with pg_dump and
     restore into the spoke. Because `seq` is emit-time in the write's
     transaction (§3.2), the dumped store carries its own stream counter — the
     snapshot provably contains every event up to that counter.
  3. SCRUB then set watermarks. The restored store is the PRIMARY's, replication
     state and all — booting on it would drain the primary's cloned outbox under
     the primary's identity (origin suppression guards the emit hook, not the
     delivery loop). So: read the restored emit counter (keyed by the primary's
     source_id = the spoke's inbound stream) and initialize the spoke's inbound
     watermark/`applied_seq` to it; TRUNCATE every cloned replication table
     EXCEPT `replication_stream_counters` (deliberately retained — inert under a
     foreign source_id); then WRITE the spoke's inbound registration (the
     receiver's handshake half, replayed after the restore so it survives it).
  4. BOOT the spoke, then PAIR THE REVERSE direction (spoke→primary) by the
     ordinary §5 handshake — the spoke authors nothing before first boot, so that
     stream needs no pre-dump half. (Boot is an ops step; `run_seed` does 1–3,
     the operator boots, then `run_reverse_pair` does 4 — see the runbook.)

Re-seeding after long divergence is the same procedure under a FRESH epoch, with
two preconditions BOTH script-checked (`check_reseed_preconditions`): the spoke's
outbox is empty/delivered AND the primary's parked set for the spoke's streams is
empty. A park ACKs as delivered, so an empty outbox does NOT imply the spoke's
writes were applied on the primary — re-seeding over an unresolved park would
overwrite the spoke's only applied copy of that write (§7 step 5).
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from snowline_platform.replication_pairing import (
    Participant,
    discover_participants,
    handshake_direction,
    mint_epoch,
)
from snowline_plugin_sdk.replication.models import (
    ReplicationInboundStream,
    ReplicationOutboxRow,
    ReplicationParkedEvent,
    ReplicationStreamCounter,
    ReplicationSubscription,
)

Report = Callable[[str], None]

# The cloned replication tables §7 step 3 TRUNCATEs before first boot — every
# table EXCEPT `replication_stream_counters` (retained: inert under a foreign
# source_id, and the counter is what step 3 reads to set the watermark). Ordered
# child-before-parent so the deletes hold under the outbox→subscription FK on any
# dialect (the SDK suite runs on SQLite, which doesn't cascade a plain DELETE).
_SCRUB_MODELS = (
    ReplicationOutboxRow,
    ReplicationSubscription,
    ReplicationInboundStream,
    ReplicationParkedEvent,
)


class SeedError(RuntimeError):
    """A seed step failed hard, or a re-seed precondition was not met."""


@dataclass(frozen=True)
class SeedParticipant:
    """One participant's seed coordinates: its live primary-side `Participant`
    (admin surface + event vocabulary, discovered from the primary), plus the
    static endpoints the seed needs that are NOT discoverable while the spoke is
    down — the spoke's ingest URL (prime targets it before boot) and the two
    Postgres URLs the dump flows between."""

    name: str
    primary: Participant
    spoke_source_id: str
    spoke_ingest_url: str
    primary_dump_url: str
    spoke_db_url: str


@dataclass(frozen=True)
class SeedConfig:
    primary_platform_url: str
    primary_instance: str
    spoke_platform_url: str
    spoke_instance: str
    participants: tuple[SeedParticipant, ...]


def load_seed_config(client, path: str | Path) -> SeedConfig:
    """Load + resolve a seed config JSON. The primary is UP during seeding, so the
    primary-side `Participant` for each named participant (admin base, ingest URL,
    event vocabulary, source_id) is DISCOVERED from the primary's `/plugins` (plus
    the platform's own scope participant, §8). The config supplies only what
    discovery can't reach: the spoke endpoints (down during prime) and the
    Postgres URLs. A participant named in the config but not opted-in on the
    primary is a hard error — you cannot seed a stream the primary won't emit."""
    from urllib.parse import urlsplit

    data = json.loads(Path(path).read_text())
    primary = data["primary"]
    spoke = data["spoke"]
    # The seed runs on the spoke box; the primary is reached over the tailnet, so
    # its plugins' loopback base_urls are rewritten onto the primary's tailnet
    # host (§4.1), same posture as the pairing CLI's --peer-host.
    primary_host = data["primary"].get("host") or urlsplit(primary["platform_url"]).hostname
    discovered = discover_participants(
        client, primary["platform_url"], primary["instance"],
        include_platform=True, reachable_host=primary_host,
    )
    participants: list[SeedParticipant] = []
    for name, pcfg in data["participants"].items():
        if name.startswith("__"):
            continue  # `__doc__` and friends are inline documentation, not participants
        prim = discovered.get(name)
        if prim is None:
            raise SeedError(
                f"participant {name!r} is in the seed config but the primary at "
                f"{primary['platform_url']} does not declare replication for it "
                f"(discovered: {sorted(discovered)}). Seed only opted-in "
                f"participants."
            )
        participants.append(
            SeedParticipant(
                name=name,
                primary=prim,
                spoke_source_id=f"{spoke['instance']}.{name}",
                spoke_ingest_url=pcfg["spoke_ingest_url"],
                primary_dump_url=pcfg["primary_dump_url"],
                spoke_db_url=pcfg["spoke_db_url"],
            )
        )
    return SeedConfig(
        primary_platform_url=primary["platform_url"],
        primary_instance=primary["instance"],
        spoke_platform_url=spoke["platform_url"],
        spoke_instance=spoke["instance"],
        participants=tuple(participants),
    )


# --- step 1: prime the primary → spoke stream ---------------------------------


def prime_forward(
    client, sp: SeedParticipant, *, report: Report = print
) -> tuple[str, str]:
    """§7 step 1 for one participant: create the PRIMARY's outbound subscription
    toward the (not-yet-booted) spoke, with a script-minted epoch + secret. The
    script plays the receiver here (the spoke's store doesn't exist) — the one
    exception to receiver-mints (§5). `peer_source_id` is wired to the spoke's
    source_id now; it reports peer_seen 0 until the reverse stream is paired
    (step 4). Returns `(epoch, secret)` for the script to carry into step 3.

    From the instant this returns, every matching primary write emits into the
    stream, so this MUST precede the dump.

    RE-RUN SAFETY: priming mints a FRESH epoch each call, so a prime that failed
    mid-sequence (or an aborted seed) leaves an ORPHANED active outbound
    subscription toward this spoke on the primary — its old-epoch deliveries
    would dead-letter against the spoke's newly-injected registration. So before
    creating the new one, retire any active outbound already targeting this
    spoke's ingest URL. Idempotent: the first, clean run finds none."""
    _retire_orphan_forward(client, sp, report=report)
    epoch = mint_epoch()
    secret = secrets.token_hex(32)
    report(
        f"[prime] {sp.primary.source_id} -> {sp.spoke_source_id} "
        f"(epoch {epoch}): creating primary outbound subscription"
    )
    resp = client.post(
        f"{sp.primary.admin_base}/outbound",
        json={
            "target_url": sp.spoke_ingest_url,
            "secret": secret,
            "event_types": list(sp.primary.events),
            "epoch": epoch,
            "source_id": sp.primary.source_id,
            "peer_source_id": sp.spoke_source_id,
        },
    )
    if resp.status_code >= 400:
        raise SeedError(
            f"priming {sp.name!r} failed: POST {sp.primary.admin_base}/outbound "
            f"-> HTTP {resp.status_code} {_safe_body(resp)}"
        )
    return epoch, secret


# --- step 2: dump + restore ---------------------------------------------------


def dump_and_restore(sp: SeedParticipant, *, report: Report = print) -> None:
    """§7 step 2 for one participant: `pg_dump` the primary store and restore it
    into the spoke's database. Custom-format dump + `pg_restore --clean
    --if-exists` so a spoke DB that already has the (alembic-created) schema is
    dropped-and-replaced cleanly rather than colliding. `--no-owner
    --no-privileges` keeps the restore host-role-agnostic (the two instances are
    different owner Macs).

    `--exit-on-error` is LOAD-BEARING: `--if-exists` already downgrades the only
    benign noise (dropping objects absent on a first restore), so without
    `--exit-on-error` pg_restore would keep going past a GENUINE error and still
    exit non-zero — a partially-restored store would then flow into scrub + boot
    as silent data loss (§7's whole failure mode). So any non-zero exit aborts
    the seed before step 3. Passwords ride PGPASSWORD, never argv (ps hygiene)."""
    dump_url, dump_env = _libpq_url_and_env(sp.primary_dump_url)
    restore_url, restore_env = _libpq_url_and_env(sp.spoke_db_url)
    with tempfile.TemporaryDirectory() as tmp:
        dump_file = Path(tmp) / f"{sp.name}.dump"
        report(f"[dump ] {sp.name}: pg_dump primary -> {dump_file.name}")
        _run(
            ["pg_dump", "-Fc", "--no-owner", "--no-privileges", "-f",
             str(dump_file), dump_url],
            what=f"pg_dump {sp.name}",
            env=dump_env,
        )
        report(f"[restore] {sp.name}: pg_restore -> spoke")
        _run(
            ["pg_restore", "--clean", "--if-exists", "--exit-on-error",
             "--no-owner", "--no-privileges", "-d", restore_url, str(dump_file)],
            what=f"pg_restore {sp.name}",
            env=restore_env,
        )


# --- step 3: scrub + set watermarks -------------------------------------------


def scrub_and_inject(
    sp: SeedParticipant, epoch: str, secret: str, *, report: Report = print
) -> int:
    """§7 step 3 for one participant, in the load-bearing sub-order: READ the
    restored emit counter for the primary's stream `(primary.source_id, epoch)`
    → N; TRUNCATE every cloned replication table EXCEPT the counters; WRITE the
    spoke's inbound registration `(primary.source_id, epoch, secret)` with its
    watermark/`applied_seq` initialized to N. Returns N (the initial watermark).

    Nothing CLONED survives the scrub; the seed-written registration is not a
    clone. The retained counters are inert — emit allocation is source_id-keyed,
    so the spoke's own outbound counters start fresh under the spoke's source_id.
    Events emitted after the dump (seq > N) wait in the primary's outbox and
    deliver normally: the snapshot-to-stream handoff is gapless and
    exactly-once."""
    engine = create_engine(_sqlalchemy_url(sp.spoke_db_url), future=True)
    try:
        with Session(engine, expire_on_commit=False) as session:
            counter = session.get(
                ReplicationStreamCounter, (sp.primary.source_id, epoch)
            )
            watermark = counter.last_seq if counter is not None else 0
            report(
                f"[scrub] {sp.name}: restored counter "
                f"({sp.primary.source_id}, {epoch}) last_seq={watermark} "
                f"-> spoke inbound watermark"
            )
            for model in _SCRUB_MODELS:
                session.execute(delete(model))
            report(
                f"[scrub] {sp.name}: truncated {len(_SCRUB_MODELS)} cloned "
                f"replication tables (counters retained)"
            )
            session.add(
                ReplicationInboundStream(
                    source_id=sp.primary.source_id,
                    epoch=epoch,
                    secret=secret,
                    gate_seq=watermark,
                    applied_seq=watermark,
                    active=True,
                )
            )
            report(
                f"[inject] {sp.name}: wrote spoke inbound registration "
                f"({sp.primary.source_id}, {epoch}) gate=applied={watermark}"
            )
            session.commit()
        return watermark
    finally:
        engine.dispose()


# --- steps 1–3 orchestrated ---------------------------------------------------


def run_seed(
    client, cfg: SeedConfig, *, report: Report = print
) -> dict[str, dict]:
    """Run §7 steps 1–3 for every configured participant, in order. Returns a
    per-participant record `{epoch, watermark}` (the secret is deliberately NOT
    returned — it lives only long enough to reach the spoke's injected
    registration, then is dropped, §7 step 1). After this, the operator BOOTS the
    spoke and runs `run_reverse_pair` (step 4)."""
    results: dict[str, dict] = {}
    for sp in cfg.participants:
        report(f"=== seeding {sp.name} ({sp.primary.source_id} -> {sp.spoke_source_id}) ===")
        epoch, secret = prime_forward(client, sp, report=report)
        dump_and_restore(sp, report=report)
        watermark = scrub_and_inject(sp, epoch, secret, report=report)
        results[sp.name] = {"epoch": epoch, "watermark": watermark}
    report(
        "seed steps 1-3 complete: primed, dumped, scrubbed + injected for "
        f"{len(results)} participant(s). BOOT the spoke, then run "
        "`snowline replicate seed --reverse-pair` (§7 step 4)."
    )
    return results


# --- step 4: pair the reverse direction ---------------------------------------


def run_reverse_pair(
    client, cfg: SeedConfig, *, report: Report = print
) -> list[dict]:
    """§7 step 4: with the spoke now booted, pair the spoke→primary direction by
    the ORDINARY §5 handshake (the primary mints, as receiver) — the spoke
    authored nothing before boot, so this stream needs no pre-dump half. The
    forward (primary→spoke) direction is already live from steps 1–3. Discovers
    the now-up spoke to resolve each participant's spoke-side admin surface."""
    spoke = discover_participants(
        client, cfg.spoke_platform_url, cfg.spoke_instance, include_platform=True
    )
    streams: list[dict] = []
    for sp in cfg.participants:
        spoke_p = spoke.get(sp.name)
        if spoke_p is None:
            raise SeedError(
                f"reverse-pair: participant {sp.name!r} is not opted-in on the "
                f"booted spoke at {cfg.spoke_platform_url} (discovered: "
                f"{sorted(spoke)}) — did the spoke boot with replication enabled?"
            )
        report(f"[reverse] pairing {sp.name}: {sp.spoke_source_id} -> {sp.primary.source_id}")
        streams.append(handshake_direction(client, spoke_p, sp.primary, report=report))
    report(f"reverse direction paired for {len(streams)} participant(s); "
           "the spoke now converges by events alone (§7 step 5).")
    return streams


# --- re-seed preconditions (§7 step 5) ----------------------------------------


def check_reseed_preconditions(
    client, cfg: SeedConfig, *, report: Report = print
) -> None:
    """Both §7-step-5 re-seed preconditions, script-checked for EVERY participant
    — raises `SeedError` on the first failure so a re-seed never runs over unsafe
    state:

      (a) the spoke's outbox is empty/DELIVERED — no `pending` row still waiting
          to reach the primary, AND no `rejected` (dead-lettered) row either: a
          rejected spoke write was refused by the primary and will NEVER apply,
          so it is NOT convergent, and re-seeding would wipe the spoke's only
          copy just as surely as a pending one; AND
      (b) the primary's parked set for the spoke's streams is empty — no
          spoke-authored event the primary received but could not apply.

    Both are required because a park ACKs as delivered (§8.1): an empty outbox
    does NOT imply the spoke's writes were applied on the primary, and re-seeding
    over an unresolved park would overwrite the spoke's only applied copy of that
    write."""
    failures: list[str] = []
    for sp in cfg.participants:
        pending, rejected = _spoke_undelivered_counts(sp)
        if pending:
            failures.append(
                f"{sp.name}: spoke has {pending} PENDING outbox row(s) still "
                f"undelivered — deliver them to the primary before re-seeding "
                f"(precondition a)"
            )
        if rejected:
            failures.append(
                f"{sp.name}: spoke has {rejected} REJECTED (dead-lettered) outbox "
                f"row(s) — the primary refused them, so they never applied and are "
                f"NOT convergent; resolve/export them before re-seeding or they are "
                f"lost (precondition a)"
            )
        parked = _primary_parked_for_spoke(client, sp)
        if parked:
            failures.append(
                f"{sp.name}: primary has {parked} parked event(s) on the spoke's "
                f"stream {sp.spoke_source_id!r} — resolve/re-apply them before "
                f"re-seeding; a park ACKs as delivered, so the empty outbox does "
                f"NOT mean they were applied (precondition b)"
            )
    if failures:
        raise SeedError(
            "re-seed preconditions NOT met (§7 step 5):\n  - "
            + "\n  - ".join(failures)
        )
    report(
        "re-seed preconditions met for all participants: spoke outbox drained "
        "AND primary parked set empty for the spoke's streams."
    )


def retire_old_streams(
    client, cfg: SeedConfig, *, report: Report = print
) -> None:
    """Before a fresh-epoch re-seed, retire the OLD streams on both sides so the
    new epoch's seq restarting at 1 can never be rejected by the old epoch's
    watermark (§7 step 5). Retires the primary's outbound forward subscriptions
    and its inbound reverse streams; the spoke's cloned state is wiped by the
    re-seed's restore + scrub regardless, so only the primary needs explicit
    retirement here. Idempotent — a missing stream is fine."""
    for sp in cfg.participants:
        _retire_primary_outbound(client, sp, report=report)
        _retire_primary_inbound_from_spoke(client, sp, report=report)


# --- helpers ------------------------------------------------------------------


def _spoke_undelivered_counts(sp: SeedParticipant) -> tuple[int, int]:
    """`(pending, rejected)` outbox-row counts on the spoke. Anything not
    `delivered` blocks a re-seed: `pending` is in flight, `rejected` is
    dead-lettered and will never apply — both mean the spoke holds the only copy
    of a non-convergent write (§7 step 5)."""
    engine = create_engine(_sqlalchemy_url(sp.spoke_db_url), future=True)
    try:
        with Session(engine) as session:
            rows = session.scalars(
                select(ReplicationOutboxRow).where(
                    ReplicationOutboxRow.status != "delivered"
                )
            ).all()
            pending = sum(1 for r in rows if r.status == "pending")
            rejected = sum(1 for r in rows if r.status == "rejected")
            return pending, rejected
    finally:
        engine.dispose()


def _primary_parked_for_spoke(client, sp: SeedParticipant) -> int:
    resp = client.get(f"{sp.primary.admin_base}/parked")
    if resp.status_code >= 400:
        raise SeedError(
            f"reading parked set for {sp.name!r} failed: "
            f"GET {sp.primary.admin_base}/parked -> HTTP {resp.status_code}"
        )
    return sum(1 for p in resp.json() if p.get("source_id") == sp.spoke_source_id)


def _retire_orphan_forward(client, sp: SeedParticipant, *, report: Report) -> None:
    """Retire any ACTIVE primary outbound subscription already targeting this
    spoke's ingest URL — the orphan a previously-aborted prime would leave. Keyed
    on `target_url` (the specific spoke stream), so a re-prime can't accumulate
    two live forward subscriptions racing deliveries under different epochs."""
    listed = client.get(f"{sp.primary.admin_base}/outbound")
    if listed.status_code >= 400:
        return
    for sub in listed.json():
        if sub.get("active") and sub.get("target_url") == sp.spoke_ingest_url:
            client.post(
                f"{sp.primary.admin_base}/outbound/retire", json={"id": sub["id"]}
            )
            report(
                f"[prime] {sp.name}: retired orphaned prior forward subscription "
                f"{sub['id']} (epoch {sub.get('epoch')}) before re-priming"
            )


def _retire_primary_outbound(client, sp: SeedParticipant, *, report: Report) -> None:
    listed = client.get(f"{sp.primary.admin_base}/outbound")
    if listed.status_code >= 400:
        return
    for sub in listed.json():
        if sub.get("source_id") == sp.primary.source_id and sub.get("active"):
            client.post(
                f"{sp.primary.admin_base}/outbound/retire", json={"id": sub["id"]}
            )
            report(f"[retire] {sp.name}: primary outbound {sub['id']} (epoch {sub.get('epoch')})")


def _retire_primary_inbound_from_spoke(
    client, sp: SeedParticipant, *, report: Report
) -> None:
    listed = client.get(f"{sp.primary.admin_base}/inbound")
    if listed.status_code >= 400:
        return
    for stream in listed.json():
        if stream.get("source_id") == sp.spoke_source_id and stream.get("active"):
            client.post(
                f"{sp.primary.admin_base}/inbound/retire",
                json={"source_id": stream["source_id"], "epoch": stream["epoch"]},
            )
            report(f"[retire] {sp.name}: primary inbound from {sp.spoke_source_id} "
                   f"(epoch {stream.get('epoch')})")


def _safe_body(resp) -> str:
    try:
        return str(resp.json())
    except Exception:  # noqa: BLE001 - non-JSON error body
        return getattr(resp, "text", "")[:200]


def _libpq_url(url: str) -> str:
    """A libpq-consumable URL for pg_dump/pg_restore: strip SQLAlchemy's
    `+psycopg` (or any `+driver`) so the CLI tools see a plain
    `postgresql://` scheme."""
    if url.startswith("postgresql+"):
        return "postgresql://" + url.split("://", 1)[1]
    if url.startswith("postgres+"):
        return "postgres://" + url.split("://", 1)[1]
    return url


def _libpq_url_and_env(url: str) -> tuple[str, dict[str, str]]:
    """A libpq URL with the password LIFTED OUT of the URL and into a `PGPASSWORD`
    env fragment, so it never rides pg_dump/pg_restore's argv (visible in `ps`).
    Returns `(url_without_password, env_overlay)`; the overlay is empty when the
    URL carries no password (peer/trust auth, or a socket connection)."""
    plain = _libpq_url(url)
    parts = urlsplit(plain)
    if not parts.password:
        return plain, {}
    userinfo = parts.username or ""
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{userinfo}@{host}" if userinfo else host
    scrubbed = urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )
    return scrubbed, {"PGPASSWORD": parts.password}


def _sqlalchemy_url(url: str) -> str:
    """A SQLAlchemy URL for the scrub/inject engine: normalize a bare
    `postgresql://` to the platform's `postgresql+psycopg://` driver so the seed
    uses the same driver the app does. Non-postgres URLs (SQLite in tests) pass
    through untouched."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.split("://", 1)[1]
    return url


def _run(
    argv: list[str],
    *,
    what: str,
    env: dict[str, str] | None = None,
    allow_returncodes: tuple[int, ...] = (0,),
) -> None:
    """Run a pg CLI tool. ANY exit code outside `allow_returncodes` (default:
    only 0) aborts with `SeedError` — the seed must never proceed past a
    partially-failed dump/restore. `env` overlays the current environment (e.g.
    a lifted `PGPASSWORD`)."""
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
    )
    if proc.returncode not in allow_returncodes:
        raise SeedError(
            f"{what} failed (exit {proc.returncode}):\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
