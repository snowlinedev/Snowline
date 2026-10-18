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
  2. SNAPSHOT each opted-in store (and the platform DB for scopes) and restore
     it into the spoke. The PRIMARY dumps ITSELF — the seed fetches the archive
     from `{admin_base}/snapshot` on the primary's own replication-admin surface
     (signed with step 1's secret) and restores it locally with `pg_restore`.
     The primary's Postgres is NEVER dialled across the tailnet: governance
     decision 1a83031c keeps it loopback-only, so every cross-instance byte
     moves over a Snowline service's HTTP surface behind the trust gate. The
     snapshot's auth is the primed subscription, which makes step 1's precedence
     mechanical rather than merely documented. Because `seq` is emit-time in the
     write's transaction (§3.2), the dumped store carries its own stream
     counter — the snapshot provably contains every event up to that counter.
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
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from snowline_platform.replication import via_base
from snowline_plugin_sdk.replication.envelope import is_backfill_source
from snowline_platform.replication_pairing import (
    PairingError,
    Participant,
    discover_participants,
    handshake_direction,
    live_outbound,
    mint_epoch,
    refuse_on_version_mismatch,
)
from snowline_plugin_sdk.replication.envelope import sign_body
from snowline_plugin_sdk.replication.models import (
    ReplicationInboundStream,
    ReplicationOutboxRow,
    ReplicationParkedEvent,
    ReplicationStreamCounter,
    ReplicationSubscription,
)

# ONE implementation of the libpq-URL/PGPASSWORD hygiene, shared with the
# service side that now produces the dump (`snapshot.run_pg_dump`) — the seed
# only restores here, but both halves must scrub passwords off argv identically.
from snowline_plugin_sdk.replication.snapshot import libpq_url_and_env, pg_tool

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


# The snapshot request OVERRIDES the CLI client's ordinary 30s timeout: the
# primary runs `pg_dump` before the first response byte, so a large store's
# think-time is minutes, not seconds. One hour is "an operator would have given
# up long ago", not a real bound.
SNAPSHOT_TIMEOUT = 3600.0


class SeedError(RuntimeError):
    """A seed step failed hard, or a re-seed precondition was not met."""


@dataclass(frozen=True)
class SeedParticipant:
    """One participant's seed coordinates: its live primary-side `Participant`
    (admin surface + event vocabulary, discovered from the primary), plus the
    static endpoints the seed needs that are NOT discoverable while the spoke is
    down — the spoke's ingest URL (prime targets it before boot) and the spoke's
    own Postgres URL (local to the machine running the seed).

    There is deliberately NO primary-side Postgres URL: the primary's snapshot
    comes from its OWN admin surface (`{primary.admin_base}/snapshot`), never
    from a `pg_dump` dialled across the tailnet — governance decision
    1a83031c."""

    name: str
    primary: Participant
    spoke_source_id: str
    spoke_ingest_url: str
    spoke_db_url: str


@dataclass(frozen=True)
class SeedConfig:
    primary_platform_url: str
    primary_instance: str
    spoke_platform_url: str
    spoke_instance: str
    participants: tuple[SeedParticipant, ...]


def load_seed_config(client, path: str | Path, *, report: Report = print) -> SeedConfig:
    """Load + resolve a seed config JSON. The primary is UP during seeding, so the
    primary-side `Participant` for each named participant (admin base, ingest URL,
    event vocabulary, source_id) is DISCOVERED from the primary's `/plugins` (plus
    the platform's own scope participant, §8). The config supplies only what
    discovery can't reach: the spoke endpoints (down during prime) and the spoke's
    Postgres URL. A participant named in the config but not opted-in on the
    primary is a hard error — you cannot seed a stream the primary won't emit.

    A `primary_dump_url` left over from an older seed.json is IGNORED with a
    warning rather than a hard failure — the primary's snapshot now comes from
    its own admin surface (decision 1a83031c), and existing seed.json files on
    operators' machines still carry the key."""
    data = json.loads(Path(path).read_text())
    primary = data["primary"]
    spoke = data["spoke"]
    # The seed runs on the spoke box; the primary is a PEER, so its plugins are
    # addressed through the primary's gateway (`<platform_url>/via/<name>`,
    # §4.1 / decision 0b8390f7) — the only primary port the spoke ever dials.
    if primary.get("host"):
        report(
            f"[warn] primary.host in {Path(path).name} is IGNORED — peers are "
            f"addressed through the primary's gateway at "
            f"{via_base(primary['platform_url'], '<plugin>')} (decision 0b8390f7); "
            f"drop the key."
        )
    discovered = discover_participants(
        client, primary["platform_url"], primary["instance"],
        include_platform=True, peer=True,
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
        if "primary_dump_url" in pcfg:
            report(
                f"[warn] {name}: 'primary_dump_url' in {Path(path).name} is "
                f"IGNORED — the primary now serves its own snapshot over "
                f"{prim.admin_base}/snapshot and its Postgres is never reachable "
                f"over the tailnet (decision 1a83031c). Drop the key."
            )
        participants.append(
            SeedParticipant(
                name=name,
                primary=prim,
                spoke_source_id=f"{spoke['instance']}.{name}",
                spoke_ingest_url=pcfg["spoke_ingest_url"],
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


def dump_and_restore(
    client, sp: SeedParticipant, epoch: str, secret: str, *, report: Report = print
) -> None:
    """§7 step 2 for one participant: FETCH the primary's snapshot over the
    primary's OWN admin surface, then restore it into the spoke's database.

    The primary dumps itself (`POST {admin_base}/snapshot`, SDK
    `admin.build_replication_router`) and streams a `pg_dump -Fc` archive back;
    the seed never dials the primary's Postgres. **Governance decision
    1a83031c**: the hub's Postgres stays loopback-only and is never exposed on
    the tailnet, not even with password auth — all cross-instance data movement
    goes through a Snowline service's HTTP surface behind the trust gate.

    The request is signed with the secret `prime_forward` just minted for the
    primary→spoke stream (§7 step 1), which is why step 1 MUST precede this
    call: the snapshot route resolves the ACTIVE outbound subscription for
    `(source_id, epoch, peer_source_id)` and verifies the HMAC against its
    secret — no prime, no snapshot. Any non-2xx (a 404 covers both "no such
    stream" and "bad signature", by design) aborts the seed BEFORE step 3.

    The restore side is unchanged: custom-format archive + `pg_restore --clean
    --if-exists` so a spoke DB that already has the (alembic-created) schema is
    dropped-and-replaced cleanly rather than colliding; `--no-owner
    --no-privileges` keeps it host-role-agnostic (the two instances are
    different owner Macs).

    `--exit-on-error` is LOAD-BEARING: `--if-exists` already downgrades the only
    benign noise (dropping objects absent on a first restore), so without
    `--exit-on-error` pg_restore would keep going past a GENUINE error and still
    exit non-zero — a partially-restored store would then flow into scrub + boot
    as silent data loss (§7's whole failure mode). So any non-zero exit aborts
    the seed before step 3. Passwords ride PGPASSWORD, never argv (ps hygiene),
    and the stream secret rides the signature header — never argv, never a
    report line, never an error body."""
    restore_url, restore_env = libpq_url_and_env(sp.spoke_db_url)
    snapshot_url = f"{sp.primary.admin_base}/snapshot"
    with tempfile.TemporaryDirectory() as tmp:
        dump_file = Path(tmp) / f"{sp.name}.dump"
        report(f"[dump ] {sp.name}: requesting snapshot from {snapshot_url}")
        size = _fetch_snapshot(client, sp, epoch, secret, dump_file)
        report(f"[dump ] {sp.name}: received {size} byte archive -> {dump_file.name}")
        report(f"[restore] {sp.name}: pg_restore -> spoke")
        _run(
            [pg_tool("pg_restore"), "--clean", "--if-exists", "--exit-on-error",
             "--no-owner", "--no-privileges", "-d", restore_url, str(dump_file)],
            what=f"pg_restore {sp.name}",
            env=restore_env,
        )


def _snapshot_request_body(sp: SeedParticipant, epoch: str) -> bytes:
    """The exact bytes the snapshot request POSTs AND signs — built once so the
    signature is computed over what actually goes on the wire (delivery-time
    signing over exact bytes, §5). Names the primed stream unambiguously:
    `(source_id, epoch)` is unique on the primary, and `peer_source_id` pins it
    to THIS spoke."""
    return json.dumps(
        {
            "source_id": sp.primary.source_id,
            "epoch": epoch,
            "peer_source_id": sp.spoke_source_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _fetch_snapshot(
    client, sp: SeedParticipant, epoch: str, secret: str, dest: Path
) -> int:
    """POST the signed snapshot request and stream the archive to `dest`.
    Returns the byte count. Streams via `client.stream` when the client offers
    it (the production `httpx.Client` does, so a multi-GB archive never lands in
    memory) and falls back to a buffered POST otherwise."""
    body = _snapshot_request_body(sp, epoch)
    headers = {
        "X-Snowline-Signature": sign_body(secret, body),
        "Content-Type": "application/json",
    }
    url = f"{sp.primary.admin_base}/snapshot"
    total = 0
    stream = getattr(client, "stream", None)
    if stream is not None:
        with stream(
            "POST", url, content=body, headers=headers, timeout=SNAPSHOT_TIMEOUT
        ) as resp:
            if resp.status_code >= 300:
                resp.read()  # a streamed error body is unread until asked for
                _raise_snapshot_error(resp, sp, url)
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
                    total += len(chunk)
    else:
        resp = client.post(url, content=body, headers=headers, timeout=SNAPSHOT_TIMEOUT)
        if resp.status_code >= 300:
            _raise_snapshot_error(resp, sp, url)
        with open(dest, "wb") as fh:
            fh.write(resp.content)
            total = len(resp.content)
    if total == 0:
        # A `pg_dump -Fc` archive is never empty, even for an empty database.
        raise SeedError(
            f"snapshot for {sp.name!r} came back EMPTY from {url} — refusing to "
            f"restore a zero-byte archive over the spoke's store"
        )
    return total


def _raise_snapshot_error(resp, sp: SeedParticipant, url: str) -> None:
    hint = ""
    if resp.status_code == 404:
        hint = (
            " — the primary answers 404 for both 'no such stream' and 'bad "
            "signature' (non-enumeration, by design). Check that step 1 primed "
            "this participant against THIS primary, and that the primary runs a "
            "build with the snapshot route (item 0ebe6a70)."
        )
    raise SeedError(
        f"fetching the snapshot for {sp.name!r} failed: POST {url} -> "
        f"HTTP {resp.status_code} {_safe_body(resp)}{hint}"
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
        dump_and_restore(client, sp, epoch, secret, report=report)
        watermark = scrub_and_inject(sp, epoch, secret, report=report)
        results[sp.name] = {"epoch": epoch, "watermark": watermark}
    report(
        "seed steps 1-3 complete: primed, dumped, scrubbed + injected for "
        f"{len(results)} participant(s). BOOT the spoke, then run "
        "`snowline replicate seed --reverse-pair` (§7 step 4)."
    )
    return results


# --- step 4: pair the reverse direction ---------------------------------------


def select_participants(cfg: SeedConfig, names: list[str] | None) -> SeedConfig:
    """Restrict `cfg` to the named participants (`--participants NAME[,NAME]`),
    in the config's own order. `None` = all. An unknown name is refused with
    the known list — a typo must never silently pair nothing."""
    if names is None:
        return cfg
    known = [sp.name for sp in cfg.participants]
    unknown = [n for n in names if n not in known]
    if unknown:
        raise SeedError(
            f"--participants: unknown participant(s) {unknown} — the seed "
            f"config knows {known}"
        )
    wanted = set(names)
    return SeedConfig(
        primary_platform_url=cfg.primary_platform_url,
        primary_instance=cfg.primary_instance,
        spoke_platform_url=cfg.spoke_platform_url,
        spoke_instance=cfg.spoke_instance,
        participants=tuple(sp for sp in cfg.participants if sp.name in wanted),
    )


def _reverse_already_paired(
    client,
    sp: SeedParticipant,
    spoke_p: Participant,
    *,
    force: bool = False,
    report: Report,
) -> bool:
    """Is the spoke→primary stream for `sp` already live?

    "Already paired" means BOTH halves of the §5 handshake are live under ONE
    epoch: the PRIMARY (receiver) holds an ACTIVE inbound stream from the
    spoke's source_id — exactly what `handshake_direction`'s run-once guard
    keys on — AND the SPOKE (sender) holds a live outbound subscription with
    that stream identity (`live_outbound`) at the same epoch. Both are read
    with the one client `run_reverse_pair` already holds (the primary's admin
    surface through its gateway, the spoke's on loopback).

    The primary half alone is NOT enough: a plain re-seed (bootstrap-spoke
    re-run) wipes the spoke's cloned replication tables, so the primary keeps
    an active inbound from the spoke that NO sender holds the secret for — a
    primary-only check would report "already paired" and the spoke's writes
    would never flow back. A half-pair (primary inbound without a matching
    spoke outbound, or vice versa — also what a handshake that died between
    its two steps leaves) is retired on both sides and the direction
    re-paired fresh — with two guards, both checked BEFORE anything is
    retired:

    - contract_version (§10): a mismatched participant is refused without
      first destroying its half-pair;
    - UNDELIVERED SPOKE WRITES: retiring a spoke outbound strands its
      pending/rejected outbox rows for good (delivery drains only ACTIVE
      subscriptions, a retired stream's rows can't be requeued, and the
      fresh-epoch stream starts with an empty outbox). So if any outbound
      about to be retired still holds undelivered rows, refuse unless
      `force` — today that state is a LOUD wedge (rejected rows, degraded
      /health); a silent heal would turn it into silent loss.

    Any failed retire is raised (never ignored: an un-retired orphan keeps
    delivering under a dead epoch). Returns True iff already paired."""
    resp = client.get(f"{sp.primary.admin_base}/inbound")
    if resp.status_code >= 400:
        raise SeedError(
            f"reading {sp.primary.admin_base}/inbound failed: HTTP "
            f"{resp.status_code} {_safe_body(resp)}"
        )
    inbound_epochs = {
        s.get("epoch")
        for s in resp.json()
        if s.get("source_id") == sp.spoke_source_id and s.get("active")
    }
    outbound = live_outbound(client, spoke_p, sp.primary)
    if inbound_epochs & {s.get("epoch") for s in outbound}:
        return True
    if not inbound_epochs and not outbound:
        return False  # never paired: nothing to heal
    refuse_on_version_mismatch(spoke_p, sp.primary)
    if outbound:
        sub_ids = [str(sub["id"]) for sub in outbound]
        try:
            pending, rejected = _spoke_undelivered_counts(sp, subscription_ids=sub_ids)
        except (SQLAlchemyError, ImportError, ValueError, OSError) as exc:
            # A missing driver or a malformed URL fails before SQLAlchemy
            # raises; keep the run's collect-and-continue contract either way.
            raise SeedError(
                f"half-pair heal refused: could not read the spoke's outbox at "
                f"{sp.spoke_db_url} to prove outbound(s) {sub_ids} hold no "
                f"undelivered rows ({exc.__class__.__name__}: {exc})"
            ) from exc
        if pending or rejected:
            if not force:
                raise SeedError(
                    f"half-pair heal refused: the spoke outbound(s) {sub_ids} "
                    f"toward {sp.primary.source_id} have no matching primary "
                    f"inbound but still hold {pending} pending + {rejected} "
                    "rejected outbox row(s) — retiring them would strand those "
                    "spoke-authored writes for good. Let the outbox drain or "
                    "resolve the spoke's /rejected rows first, or pass --force "
                    "to retire anyway and accept the loss"
                )
            report(
                f"[reverse] {sp.name}: --force: retiring spoke outbound(s) "
                f"{sub_ids} DESPITE {pending} pending + {rejected} rejected "
                "undelivered row(s) — those writes will not reach the primary"
            )
    if inbound_epochs:
        report(
            f"[reverse] {sp.name}: primary holds an active inbound from "
            f"{sp.spoke_source_id} with no matching live spoke outbound (a "
            "half-pair: re-seeded spoke or an interrupted handshake) — "
            "retiring it before re-pairing"
        )
        _retire_primary_inbound_from_spoke(client, sp, report=report, strict=True)
    for sub in outbound:
        r = client.post(f"{spoke_p.admin_base}/outbound/retire", json={"id": sub["id"]})
        if r.status_code >= 400:
            raise SeedError(
                f"retiring orphaned spoke outbound {sub['id']} failed: POST "
                f"{spoke_p.admin_base}/outbound/retire -> HTTP {r.status_code} "
                f"{_safe_body(r)}"
            )
        report(
            f"[reverse] {sp.name}: retired orphaned spoke outbound {sub['id']} "
            f"(epoch {sub.get('epoch')}) with no matching primary inbound"
        )
    return False


def run_reverse_pair(
    client,
    cfg: SeedConfig,
    *,
    participants: list[str] | None = None,
    force: bool = False,
    report: Report = print,
) -> list[dict]:
    """§7 step 4: with the spoke now booted, pair the spoke→primary direction by
    the ORDINARY §5 handshake (the primary mints, as receiver) — the spoke
    authored nothing before boot, so this stream needs no pre-dump half. The
    forward (primary→spoke) direction is already live from steps 1–3. Discovers
    the now-up spoke to resolve each participant's spoke-side admin surface.

    RESUMABLE + IDEMPOTENT (#228): every participant is attempted. One whose
    reverse stream is already live (`_reverse_already_paired`) is reported
    "already paired" and skipped — so a re-run never trips
    `handshake_direction`'s run-once guard — and a participant that is missing
    on the spoke or fails its handshake is COLLECTED, not raised on the spot:
    everything pairable is paired first, then ONE `SeedError` names every
    participant left unpaired. A second run over a fully-paired spoke reports
    each participant "already paired" and returns normally. `participants`
    (`--participants NAME[,NAME]`) restricts the run to those names; `force`
    (`--force`) lets a half-pair heal retire a spoke outbound that still holds
    undelivered rows (see `_reverse_already_paired`). Returns the records of
    the streams opened by THIS run."""
    cfg = select_participants(cfg, participants)
    spoke = discover_participants(
        client, cfg.spoke_platform_url, cfg.spoke_instance, include_platform=True
    )
    streams: list[dict] = []
    already: list[str] = []
    failures: list[str] = []
    for sp in cfg.participants:
        spoke_p = spoke.get(sp.name)
        if spoke_p is None:
            failures.append(
                f"{sp.name!r} is not opted-in on the booted spoke at "
                f"{cfg.spoke_platform_url} (discovered: {sorted(spoke)}) — did it "
                "boot with replication enabled, and has its registration "
                "heartbeat landed?"
            )
            continue
        try:
            if _reverse_already_paired(
                client, sp, spoke_p, force=force, report=report
            ):
                report(
                    f"[reverse] {sp.name}: already paired "
                    f"({sp.spoke_source_id} -> {sp.primary.source_id})"
                )
                already.append(sp.name)
                continue
            report(
                f"[reverse] pairing {sp.name}: {sp.spoke_source_id} -> "
                f"{sp.primary.source_id}"
            )
            streams.append(
                handshake_direction(client, spoke_p, sp.primary, report=report)
            )
        except (PairingError, SeedError) as exc:
            failures.append(f"{sp.name!r}: {exc}")
    report(
        f"reverse direction: paired {len(streams)}, already paired "
        f"{len(already)}, failed {len(failures)} participant(s)."
    )
    if failures:
        raise SeedError(
            "reverse-pair incomplete — "
            + "; ".join(failures)
            + ". Everything else was paired; fix the above and re-run "
            "`snowline replicate seed --reverse-pair` (idempotent: paired "
            "participants are skipped), or restrict it with `--participants "
            "NAME[,NAME]`."
        )
    report("the spoke now converges by events alone (§7 step 5).")
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


def _spoke_undelivered_counts(
    sp: SeedParticipant, *, subscription_ids: list[str] | None = None
) -> tuple[int, int]:
    """`(pending, rejected)` outbox-row counts on the spoke. Anything not
    `delivered` blocks a re-seed: `pending` is in flight, `rejected` is
    dead-lettered and will never apply — both mean the spoke holds the only copy
    of a non-convergent write (§7 step 5)."""
    engine = create_engine(_sqlalchemy_url(sp.spoke_db_url), future=True)
    try:
        with Session(engine) as session:
            rows = session.scalars(
                _undelivered_query(subscription_ids)
            ).all()
            pending = sum(1 for r in rows if r.status == "pending")
            rejected = sum(1 for r in rows if r.status == "rejected")
            return pending, rejected
    finally:
        engine.dispose()


def _undelivered_query(subscription_ids: list[str] | None):
    """Non-delivered outbox rows, optionally narrowed to the given outbound
    subscription ids (the half-pair heal's about-to-be-retired set)."""
    q = select(ReplicationOutboxRow).where(ReplicationOutboxRow.status != "delivered")
    if subscription_ids is not None:
        q = q.where(
            ReplicationOutboxRow.subscription_id.in_(
                [uuid.UUID(i) for i in subscription_ids]
            )
        )
    return q


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
            if is_backfill_source(sub.get("source_id")):
                # §7.1 mutual exclusion, ENFORCED: a union backfill targets the
                # same ingest URL, and silently retiring it would strand the
                # union mid-walk. Never seed while a union is in flight.
                raise SeedError(
                    f"[prime] {sp.name}: a §7.1 union backfill stream "
                    f"{sub['source_id']} (epoch {sub.get('epoch')}) is active "
                    f"toward {sp.spoke_ingest_url} — never run `replicate seed` "
                    "while a union is in flight; finish it (or retire its "
                    "backfill streams on both halves) first"
                )
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
    client, sp: SeedParticipant, *, report: Report, strict: bool = False
) -> None:
    """Retire the primary's active inbound stream(s) from the spoke. `strict`
    (the reverse-pair heal) raises on a failed list/retire instead of
    skipping — a silent failure there trips the handshake's run-once guard
    right after reporting the retire."""
    listed = client.get(f"{sp.primary.admin_base}/inbound")
    if listed.status_code >= 400:
        if strict:
            raise SeedError(
                f"GET {sp.primary.admin_base}/inbound -> HTTP "
                f"{listed.status_code} {_safe_body(listed)}"
            )
        return
    for stream in listed.json():
        if stream.get("source_id") == sp.spoke_source_id and stream.get("active"):
            r = client.post(
                f"{sp.primary.admin_base}/inbound/retire",
                json={"source_id": stream["source_id"], "epoch": stream["epoch"]},
            )
            if strict and r.status_code >= 400:
                raise SeedError(
                    f"retiring the primary inbound from {sp.spoke_source_id} "
                    f"(epoch {stream.get('epoch')}) failed: POST "
                    f"{sp.primary.admin_base}/inbound/retire -> HTTP "
                    f"{r.status_code} {_safe_body(r)}"
                )
            report(f"[retire] {sp.name}: primary inbound from {sp.spoke_source_id} "
                   f"(epoch {stream.get('epoch')})")


def _safe_body(resp) -> str:
    try:
        return str(resp.json())
    except Exception:  # noqa: BLE001 - non-JSON error body
        return getattr(resp, "text", "")[:200]


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
