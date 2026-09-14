"""`snowline replicate seed` (replication-continuity §7/§10, issue #82): the seed
library exercised without a live Postgres. `pg_dump`/`pg_restore` (step 2) are
covered by the real-Postgres drill; here they are faked at the `subprocess` seam
(and for the convergence tests the restore is SIMULATED by cloning the primary's
store into the spoke, which is what a restore produces), so the load-bearing
logic — the priming, the §7-step-3 scrub-then-inject, the gapless
snapshot-to-stream handoff, and both re-seed preconditions — is unit-tested on
SQLite.

Step 2 now runs over HTTP (item 0ebe6a70 / #221, governance decision 1a83031c):
the PRIMARY dumps its own database and serves the archive from its
replication-admin surface, authorized by the secret step 1 minted. The tests
below drive that end to end through the `RoutedClient` harness — the real SDK
route on the primary's real app — with only `subprocess` faked.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from snowline_platform import replication_seed as seed
from snowline_platform.replication_pairing import Participant
from snowline_plugin_sdk.replication import emit as emit_mod
from snowline_plugin_sdk.replication.envelope import sign_body
from snowline_plugin_sdk.replication.models import (
    ReplicationInboundStream,
    ReplicationOutboxRow,
    ReplicationParkedEvent,
    ReplicationStreamCounter,
    ReplicationSubscription,
)

from ._replication_helpers import RoutedClient, make_participant


def _clone_store(src_engine, dst_engine) -> None:
    """Simulate §7 step 2's pg_dump/restore: copy every replication row from the
    primary's store into the spoke's (the restore produces a byte-clone,
    replication state and all — which step 3 then scrubs)."""
    models = (
        ReplicationSubscription,
        ReplicationOutboxRow,
        ReplicationStreamCounter,
        ReplicationInboundStream,
        ReplicationParkedEvent,
    )
    with Session(src_engine) as src, Session(dst_engine) as dst:
        for model in models:
            for row in src.scalars(select(model)).all():
                data = {c.name: getattr(row, c.name) for c in model.__table__.columns}
                dst.merge(model(**data))
        dst.commit()


def _seed_participant(tmp_path, name="governance"):
    """A primary participant (admin app + store) and an empty spoke store on a
    file DB, wired into a SeedParticipant + a RoutedClient."""
    primary = make_participant()
    spoke_db = f"sqlite:///{tmp_path}/{name}-spoke.db"
    spoke = make_participant(db_url=spoke_db)
    sp = seed.SeedParticipant(
        name=name,
        primary=Participant(
            name=name,
            admin_base="http://prim-gov/replication-admin",
            ingest_url="http://prim-gov/events/ingest",
            source_id="primary.governance",
            events=("decision.recorded",),
            contract_version=2,
        ),
        spoke_source_id="roam.governance",
        spoke_ingest_url="http://roam-gov/events/ingest",
        spoke_db_url=spoke_db,
    )
    client = RoutedClient({"prim-gov": primary.app, "roam-gov": spoke.app})
    return sp, primary, spoke, client


def test_prime_forward_creates_primary_outbound(tmp_path):
    sp, primary, _spoke, client = _seed_participant(tmp_path)
    epoch, secret = seed.prime_forward(client, sp, report=lambda _m: None)
    assert epoch and len(secret) == 64  # token_hex(32)
    with primary.scope() as s:
        sub = s.scalars(select(ReplicationSubscription)).one()
    assert sub.source_id == "primary.governance"
    assert sub.epoch == epoch
    assert sub.target_url == "http://roam-gov/events/ingest"
    assert sub.peer_source_id == "roam.governance"  # wired to the spoke stream
    assert list(sub.event_types) == ["decision.recorded"]


def test_scrub_and_inject_sets_watermark_and_wipes_clones(tmp_path):
    sp, primary, spoke, client = _seed_participant(tmp_path)
    epoch, secret = seed.prime_forward(client, sp, report=lambda _m: None)

    # Primary authors 3 writes AFTER priming, BEFORE the dump → counter = 3.
    _emit(primary, "primary.governance", 3, "decision.recorded")
    _clone_store(primary.engine, spoke.engine)

    # The clone carried the primary's OWN outbound subscription + outbox +
    # counter into the spoke — booting on those would be corruption (§7 step 3).
    with spoke.scope() as s:
        assert s.scalars(select(ReplicationSubscription)).all()  # cloned junk
        assert s.get(ReplicationStreamCounter, ("primary.governance", epoch)).last_seq == 3

    watermark = seed.scrub_and_inject(sp, epoch, secret, report=lambda _m: None)
    assert watermark == 3

    with spoke.scope() as s:
        # Cloned replication tables wiped...
        assert s.scalars(select(ReplicationSubscription)).all() == []
        assert s.scalars(select(ReplicationOutboxRow)).all() == []
        # ...counter RETAINED (deliberately outside the scrub set, §7)...
        assert s.get(ReplicationStreamCounter, ("primary.governance", epoch)).last_seq == 3
        # ...and the spoke's inbound registration written at watermark 3.
        inbound = s.scalars(select(ReplicationInboundStream)).one()
    assert inbound.source_id == "primary.governance"
    assert inbound.epoch == epoch
    assert inbound.secret == secret
    assert inbound.gate_seq == 3 and inbound.applied_seq == 3
    assert inbound.active is True


def test_gapless_exactly_once_handoff(tmp_path):
    """§10's headline seeding criterion: a primary write between priming and the
    dump, and another after the dump, each reach the spoke EXACTLY once — the
    pre-dump ones as no-op duplicates (already in the snapshot), the post-dump
    one applied via the stream."""
    sp, primary, spoke, client = _seed_participant(tmp_path)
    epoch, secret = seed.prime_forward(client, sp, report=lambda _m: None)

    _emit(primary, "primary.governance", 3, "decision.recorded")  # pre-dump: seq 1-3
    _clone_store(primary.engine, spoke.engine)
    seed.scrub_and_inject(sp, epoch, secret, report=lambda _m: None)
    _emit(primary, "primary.governance", 1, "decision.recorded")  # post-dump: seq 4

    # The primary's delivery loop drains its whole outbox (seq 1-4) to the spoke.
    with primary.scope() as s:
        emit_mod.deliver_pending(s, client, reachability={})

    # seq 1-3 were in the snapshot → duplicate no-ops; only seq 4 applied.
    applied_seqs = [e["seq"] for e in spoke.applied]
    assert applied_seqs == [4]
    with spoke.scope() as s:
        stream = s.scalars(select(ReplicationInboundStream)).one()
    assert stream.gate_seq == 4 and stream.applied_seq == 4


def test_reseed_precondition_a_spoke_outbox_must_be_empty(tmp_path):
    sp, primary, spoke, client = _seed_participant(tmp_path)
    cfg = _cfg([sp])
    # A pending spoke→primary outbox row (an undelivered spoke write).
    with spoke.scope() as s:
        s.add(ReplicationSubscription(
            target_url="http://prim/x", secret="s", event_types=["decision.recorded"],
            source_id="roam.governance", epoch="e", active=True,
        ))
        s.flush()
        sub_id = s.scalars(select(ReplicationSubscription)).one().id
        s.add(ReplicationOutboxRow(
            subscription_id=sub_id, seq=1, event_type="decision.recorded",
            payload={}, status="pending",
        ))
    with pytest.raises(seed.SeedError, match="PENDING outbox"):
        seed.check_reseed_preconditions(client, cfg, report=lambda _m: None)


def test_reseed_precondition_a_rejects_dead_lettered_outbox(tmp_path):
    """A `rejected` (dead-lettered) spoke write was refused by the primary and
    will never apply — an empty-of-pending outbox does NOT make it convergent, so
    the precondition must fail on it too (review finding: status=='pending' alone
    let a dead-lettered write slip past into a data-losing re-seed)."""
    sp, primary, spoke, client = _seed_participant(tmp_path)
    cfg = _cfg([sp])
    with spoke.scope() as s:
        s.add(ReplicationSubscription(
            target_url="http://prim/x", secret="s", event_types=["decision.recorded"],
            source_id="roam.governance", epoch="e", active=True,
        ))
        s.flush()
        sub_id = s.scalars(select(ReplicationSubscription)).one().id
        s.add(ReplicationOutboxRow(
            subscription_id=sub_id, seq=1, event_type="decision.recorded",
            payload={}, status="rejected",
        ))
    with pytest.raises(seed.SeedError, match="REJECTED"):
        seed.check_reseed_preconditions(client, cfg, report=lambda _m: None)


def test_reseed_precondition_a_ignores_delivered_outbox(tmp_path):
    """A `delivered` outbox row is convergent — it must NOT block a re-seed."""
    sp, primary, spoke, client = _seed_participant(tmp_path)
    cfg = _cfg([sp])
    with spoke.scope() as s:
        s.add(ReplicationSubscription(
            target_url="http://prim/x", secret="s", event_types=["decision.recorded"],
            source_id="roam.governance", epoch="e", active=True,
        ))
        s.flush()
        sub_id = s.scalars(select(ReplicationSubscription)).one().id
        s.add(ReplicationOutboxRow(
            subscription_id=sub_id, seq=1, event_type="decision.recorded",
            payload={}, status="delivered",
        ))
    seed.check_reseed_preconditions(client, cfg, report=lambda _m: None)  # no raise


def test_reseed_precondition_b_primary_parked_must_be_empty(tmp_path):
    sp, primary, spoke, client = _seed_participant(tmp_path)
    cfg = _cfg([sp])
    # A parked event on the primary for the SPOKE's stream — an empty outbox does
    # NOT imply this was applied (a park ACKs as delivered, §8.1).
    with primary.scope() as s:
        s.add(ReplicationParkedEvent(
            source_id="roam.governance", epoch="e", seq=5,
            event_type="decision.recorded", payload={}, reason="unknown slug",
        ))
    with pytest.raises(seed.SeedError, match="parked event"):
        seed.check_reseed_preconditions(client, cfg, report=lambda _m: None)


def test_reseed_preconditions_pass_when_clean(tmp_path):
    sp, primary, spoke, client = _seed_participant(tmp_path)
    cfg = _cfg([sp])
    seed.check_reseed_preconditions(client, cfg, report=lambda _m: None)  # no raise


def test_load_seed_config_resolves_primary_from_discovery(tmp_path):
    from ._replication_helpers import make_platform, plugin_entry

    prim_platform = make_platform(plugins=[
        plugin_entry("governance", "http://127.0.0.1:8801",
                     events=["decision.recorded"]),
    ])
    client = RoutedClient({"prim-platform": prim_platform.app})
    config = {
        "primary": {"platform_url": "http://prim-platform", "instance": "primary"},
        "spoke": {"platform_url": "http://roam-platform", "instance": "roam"},
        "participants": {
            "governance": {
                "spoke_ingest_url": "http://roam-gov/events/ingest",
                "spoke_db_url": "postgresql:///gov_roam",
            },
        },
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(config))
    cfg = seed.load_seed_config(client, path)
    assert cfg.primary_instance == "primary" and cfg.spoke_instance == "roam"
    (gov,) = cfg.participants
    assert gov.primary.source_id == "primary.governance"
    # The primary's plugin is addressed THROUGH THE PRIMARY'S GATEWAY
    # (`<platform_url>/via/<name>`, §4.1 / decision 0b8390f7) — never at its
    # own loopback port across the tailnet.
    assert gov.primary.admin_base == "http://prim-platform/via/governance/replication-admin"
    assert gov.primary.ingest_url == "http://prim-platform/via/governance/events/ingest"
    assert gov.spoke_source_id == "roam.governance"


def test_load_seed_config_rejects_non_opted_in_participant(tmp_path):
    from ._replication_helpers import make_platform

    prim_platform = make_platform(plugins=[])  # governance NOT opted in
    client = RoutedClient({"prim-platform": prim_platform.app})
    config = {
        "primary": {"platform_url": "http://prim-platform", "instance": "primary"},
        "spoke": {"platform_url": "http://roam-platform", "instance": "roam"},
        "participants": {"governance": {
            "spoke_ingest_url": "http://roam-gov/events/ingest",
            "spoke_db_url": "y",
        }},
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(config))
    with pytest.raises(seed.SeedError, match="does not declare replication"):
        seed.load_seed_config(client, path)


def test_sqlalchemy_url_normalization():
    assert seed._sqlalchemy_url("postgresql:///db") == "postgresql+psycopg:///db"
    assert seed._sqlalchemy_url("sqlite:///x.db") == "sqlite:///x.db"


# --- §7 step 2 over HTTP (item 0ebe6a70, decision 1a83031c) -------------------


def _fake_pg(
    monkeypatch,
    *,
    archive: bytes = b"PGDMP-fake-archive",
    restore_rc: int = 0,
    restored: list[bytes] | None = None,
):
    """One fake for BOTH pg CLI tools — `pg_dump` (run by the primary's snapshot
    route, which in this in-process harness shares our `subprocess` module) and
    `pg_restore` (run by the seed). Returns the recorded argv list; `restored`
    collects the bytes each pg_restore was handed."""
    calls: list[list[str]] = []
    restored = restored if restored is not None else []

    class _Proc:
        def __init__(self, rc: int):
            self.returncode = rc
            self.stdout = ""
            self.stderr = "pg_restore: error: could not execute query" if rc else ""

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[0] == "pg_dump":
            Path(argv[argv.index("-f") + 1]).write_bytes(archive)
            return _Proc(0)
        # Read the archive back here: the seed deletes its temp dir on the way
        # out, so this is the only moment the test can see what was restored.
        restored.append(Path(argv[-1]).read_bytes())
        return _Proc(restore_rc)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_dump_and_restore_fetches_the_snapshot_over_http(tmp_path, monkeypatch):
    """The primary dumps ITSELF and serves the archive over its own admin
    surface; the seed fetches it over HTTP and only restores. Nothing dials the
    primary's Postgres — decision 1a83031c."""
    sp, _primary, _spoke, client = _seed_participant(tmp_path)
    epoch, secret = seed.prime_forward(client, sp, report=lambda _m: None)
    restored: list[bytes] = []
    calls = _fake_pg(
        monkeypatch, archive=b"PGDMP-primary-governance", restored=restored
    )

    seed.dump_and_restore(client, sp, epoch, secret, report=lambda _m: None)

    # The dump ran on the PRIMARY's side of the wire (inside its snapshot
    # route), the restore on the spoke's — and no URL of the primary's Postgres
    # was ever passed to a client-side tool.
    assert [c[0] for c in calls] == ["pg_dump", "pg_restore"]
    dump_argv, restore_argv = calls
    assert dump_argv[:4] == ["pg_dump", "-Fc", "--no-owner", "--no-privileges"]
    assert "--exit-on-error" in restore_argv
    assert "--clean" in restore_argv and "--if-exists" in restore_argv
    # The bytes the primary streamed back are exactly what pg_restore was fed.
    assert restore_argv[-1].endswith("governance.dump")
    assert restored == [b"PGDMP-primary-governance"]


def test_dump_and_restore_aborts_when_the_snapshot_is_refused(tmp_path, monkeypatch):
    """A non-2xx from the snapshot route aborts the seed BEFORE step 3. Here the
    stream was never primed, so the route 404s (non-enumeration) — and the seed
    must not proceed to a restore."""
    sp, _primary, _spoke, client = _seed_participant(tmp_path)
    calls = _fake_pg(monkeypatch)

    with pytest.raises(seed.SeedError, match="HTTP 404"):
        seed.dump_and_restore(
            client, sp, "never-primed-epoch", "wrong-secret", report=lambda _m: None
        )
    assert calls == []  # neither dumped nor restored


def test_dump_and_restore_aborts_on_a_bad_signature(tmp_path, monkeypatch):
    """The stream IS primed, but the request is signed with the wrong secret.
    Same 404, same abort — the seed cannot tell (and neither can an attacker)."""
    sp, _primary, _spoke, client = _seed_participant(tmp_path)
    epoch, _secret = seed.prime_forward(client, sp, report=lambda _m: None)
    calls = _fake_pg(monkeypatch)

    with pytest.raises(seed.SeedError, match="HTTP 404"):
        seed.dump_and_restore(client, sp, epoch, "not-the-secret", report=lambda _m: None)
    assert calls == []


def test_dump_and_restore_aborts_on_pg_restore_error(tmp_path, monkeypatch):
    """Review finding: `pg_restore --exit-on-error`, and ANY non-zero exit aborts
    the seed BEFORE scrub/boot — a partially-restored store must never proceed
    (silent data loss). `--if-exists` already downgrades the only benign noise,
    so the old (0,1) window admitted genuine restore failures."""
    sp, _primary, _spoke, client = _seed_participant(tmp_path)
    epoch, secret = seed.prime_forward(client, sp, report=lambda _m: None)
    calls = _fake_pg(monkeypatch, restore_rc=1)

    with pytest.raises(seed.SeedError, match="pg_restore"):
        seed.dump_and_restore(client, sp, epoch, secret, report=lambda _m: None)

    restore_argv = next(c for c in calls if c[0] == "pg_restore")
    assert "--exit-on-error" in restore_argv
    assert "--clean" in restore_argv and "--if-exists" in restore_argv


def test_dump_and_restore_refuses_an_empty_archive(tmp_path, monkeypatch):
    """A `pg_dump -Fc` archive is never zero bytes; a 200 with an empty body is
    a broken primary, not an empty database — refuse rather than wipe the spoke's
    store with nothing."""
    sp, _primary, _spoke, client = _seed_participant(tmp_path)
    epoch, secret = seed.prime_forward(client, sp, report=lambda _m: None)
    calls = _fake_pg(monkeypatch, archive=b"")

    with pytest.raises(seed.SeedError, match="EMPTY"):
        seed.dump_and_restore(client, sp, epoch, secret, report=lambda _m: None)
    assert [c[0] for c in calls] == ["pg_dump"]  # never reached pg_restore


def test_run_seed_end_to_end_over_the_http_snapshot(tmp_path, monkeypatch):
    """§10's headline seeding criterion again, but driven by `run_seed` itself
    and through the PRIMARY's REAL snapshot route: prime → signed snapshot
    request → restore → scrub/inject, then a post-dump write converging by the
    stream. The auth on the snapshot is what makes step 1's precedence
    mechanical — if priming had not happened, this run could not have dumped."""
    sp, primary, spoke, client = _seed_participant(tmp_path)

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        if argv[0] == "pg_dump":
            # The primary authors 3 writes AFTER priming, BEFORE the dump —
            # they land in the snapshot AND in the primed stream's outbox.
            _emit(primary, "primary.governance", 3, "decision.recorded")
            Path(argv[argv.index("-f") + 1]).write_bytes(b"PGDMP-clone")
            return _Proc()
        # What a real `pg_restore` of that archive produces: a byte-clone of the
        # primary's store, replication state and all (which step 3 then scrubs).
        assert Path(argv[-1]).read_bytes() == b"PGDMP-clone"
        _clone_store(primary.engine, spoke.engine)
        return _Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    results = seed.run_seed(client, _cfg([sp]), report=lambda _m: None)
    assert results["governance"]["watermark"] == 3

    _emit(primary, "primary.governance", 1, "decision.recorded")  # post-dump: seq 4
    with primary.scope() as s:
        emit_mod.deliver_pending(s, client, reachability={})
    assert [e["seq"] for e in spoke.applied] == [4]  # 1-3 were duplicates


def test_snapshot_fetch_never_leaks_the_stream_secret(tmp_path, monkeypatch):
    """The stream secret authorizes the snapshot — it must appear in the
    signature header and NOWHERE else: not in the request body, not in a report
    line, not in an error message."""
    sp, _primary, _spoke, client = _seed_participant(tmp_path)
    epoch, secret = seed.prime_forward(client, sp, report=lambda _m: None)
    assert secret not in seed._snapshot_request_body(sp, epoch).decode()

    _fake_pg(monkeypatch)
    lines: list[str] = []
    seed.dump_and_restore(client, sp, epoch, secret, report=lines.append)
    assert lines and not any(secret in line for line in lines)

    with pytest.raises(seed.SeedError) as exc:
        seed.dump_and_restore(client, sp, epoch, "a-wrong-secret", report=lambda _m: None)
    assert "a-wrong-secret" not in str(exc.value)


def test_fetch_snapshot_uses_client_stream_when_offered(tmp_path):
    """Production drives an `httpx.Client`, which streams — a multi-GB archive
    must never be buffered whole. The buffered `.post` path is the fallback."""
    sp, _primary, _spoke, _c = _seed_participant(tmp_path)
    dest = tmp_path / "streamed.dump"
    fake = _StreamingClient([b"PGD", b"MP-", b"chunks"])
    size = seed._fetch_snapshot(fake, sp, "epoch-1", "s3cret", dest)
    assert size == 12 and dest.read_bytes() == b"PGDMP-chunks"
    assert fake.calls[0][0] == "POST"
    assert fake.calls[0][1] == f"{sp.primary.admin_base}/snapshot"
    headers = fake.calls[0][2]["headers"]
    assert headers["X-Snowline-Signature"] == sign_body(
        "s3cret", seed._snapshot_request_body(sp, "epoch-1")
    )


def test_load_seed_config_warns_but_ignores_a_legacy_primary_dump_url(tmp_path):
    """Older seed.json files on operators' machines still carry the key. It is
    ignored with a warning, never a hard failure."""
    from ._replication_helpers import make_platform, plugin_entry

    prim_platform = make_platform(plugins=[
        plugin_entry("governance", "http://127.0.0.1:8801", events=["decision.recorded"]),
    ])
    client = RoutedClient({"prim-platform": prim_platform.app})
    path = tmp_path / "seed.json"
    path.write_text(json.dumps({
        "primary": {"platform_url": "http://prim-platform", "instance": "primary"},
        "spoke": {"platform_url": "http://roam-platform", "instance": "roam"},
        "participants": {"governance": {
            "spoke_ingest_url": "http://roam-gov/events/ingest",
            "primary_dump_url": "postgresql://sean@mini.ts.net:5432/snowline_governance",
            "spoke_db_url": "postgresql:///gov_roam",
        }},
    }))
    lines: list[str] = []
    cfg = seed.load_seed_config(client, path, report=lines.append)
    (gov,) = cfg.participants
    assert not hasattr(gov, "primary_dump_url")
    assert any("primary_dump_url" in line and "IGNORED" in line for line in lines)


def test_prime_forward_retires_orphan_on_rerun(tmp_path):
    """Review finding: a prime that failed mid-sequence then re-runs mints a fresh
    epoch; without a guard it leaves an orphaned old outbound subscription whose
    old-epoch deliveries dead-letter. The re-run must retire the orphan first."""
    sp, primary, _spoke, client = _seed_participant(tmp_path)
    e1, _ = seed.prime_forward(client, sp, report=lambda _m: None)
    e2, _ = seed.prime_forward(client, sp, report=lambda _m: None)
    assert e1 != e2
    with primary.scope() as s:
        subs = s.scalars(select(ReplicationSubscription)).all()
    active = [x for x in subs if x.active]
    assert len(active) == 1 and active[0].epoch == e2  # only the fresh one is live
    assert any((not x.active) and x.epoch == e1 for x in subs)  # orphan retired, not deleted


def test_run_reverse_pair_refuses_version_mismatch(tmp_path):
    """Review finding: the §7 reverse-pair reached the handshake directly,
    bypassing the §10 contract_version refuse. It must refuse a mismatch too."""
    from snowline_platform.replication_pairing import PairingError

    from ._replication_helpers import make_platform, plugin_entry

    sp, primary, _spoke, _c = _seed_participant(tmp_path)  # primary gov contract_version=2
    roam_platform = make_platform(plugins=[
        plugin_entry("governance", "http://roam-gov", contract_version=3,
                     events=["decision.recorded"]),
    ])
    roam_gov = make_participant()
    client = RoutedClient({
        "prim-gov": primary.app, "roam-platform": roam_platform.app, "roam-gov": roam_gov.app,
    })
    with pytest.raises(PairingError, match="contract_version mismatch"):
        seed.run_reverse_pair(client, _cfg([sp]), report=lambda _m: None)


# --- helpers ------------------------------------------------------------------


class _StreamingClient:
    """The minimum an `httpx.Client` offers the snapshot fetch: a `.stream`
    context manager yielding a response with `.status_code` + `.iter_bytes()`."""

    def __init__(self, chunks: list[bytes], status_code: int = 200):
        self.chunks = chunks
        self.status_code = status_code
        self.calls: list[tuple] = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        chunks, status = self.chunks, self.status_code

        class _Resp:
            status_code = status

            def read(self):
                return b"".join(chunks)

            def iter_bytes(self):
                yield from chunks

        @contextmanager
        def _cm():
            yield _Resp()

        return _cm()


def _emit(inst, source_id, n, event_type):
    # Scoped env write (#125): a raw os.environ write here leaked
    # SNOWLINE_REPLICATION_SOURCE_ID into every later-collected suite, breaking
    # governance's replication tests (whose emitters fail-loud on an unset var
    # and record the env source into LWW coordinates) under combined runs.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SNOWLINE_REPLICATION_SOURCE_ID", source_id)
        with inst.scope() as s:
            for i in range(n):
                emit_mod.emit_event(s, event_type, {"id": f"{source_id}-{i}"})


def _cfg(participants):
    return seed.SeedConfig(
        primary_platform_url="http://prim-platform",
        primary_instance="primary",
        spoke_platform_url="http://roam-platform",
        spoke_instance="roam",
        participants=tuple(participants),
    )


def test_seed_end_to_end_through_both_gateways_via_proxy(tmp_path, monkeypatch):
    """Decision 0b8390f7: the whole §7 seed — discovery, prime, the SIGNED
    snapshot request and its streamed archive, and the post-dump delivery —
    crosses instances ONLY through each side's platform gateway (`/via/<name>`).
    Neither plugin's own host is ever dialled by the peer: the primary's
    governance is reached at `prim-platform/via/governance/…` and the spoke's
    at `roam-platform/via/governance/…`. The HMAC over the exact body must
    survive the proxy hop in both directions, or the snapshot is refused and
    the delivery dead-letters."""
    import json

    from ._replication_helpers import make_platform, plugin_entry

    primary = make_participant()
    spoke_db = f"sqlite:///{tmp_path}/gov-spoke.db"
    spoke = make_participant(db_url=spoke_db)
    prim_platform = make_platform(plugins=[
        plugin_entry("governance", "http://prim-gov", events=["decision.recorded"]),
    ])
    roam_platform = make_platform(plugins=[
        plugin_entry("governance", "http://roam-gov", events=["decision.recorded"]),
    ])
    client = RoutedClient({
        "prim-platform": prim_platform.app, "prim-gov": primary.app,
        "roam-platform": roam_platform.app, "roam-gov": spoke.app,
    })
    dialled: list[str] = []
    real_request = client._request

    def spy(method, url, **kw):
        dialled.append(url)
        return real_request(method, url, **kw)

    monkeypatch.setattr(client, "_request", spy)

    config = {
        "primary": {"platform_url": "http://prim-platform", "instance": "primary"},
        "spoke": {"platform_url": "http://roam-platform", "instance": "roam"},
        "participants": {"governance": {
            "spoke_ingest_url": "http://roam-platform/via/governance/events/ingest",
            "spoke_db_url": spoke_db,
        }},
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(config))
    cfg = seed.load_seed_config(client, path, report=lambda _m: None)
    (sp,) = [p for p in cfg.participants if p.name == "governance"]
    assert sp.primary.admin_base == "http://prim-platform/via/governance/replication-admin"

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        if argv[0] == "pg_dump":
            _emit(primary, "primary.governance", 3, "decision.recorded")
            Path(argv[argv.index("-f") + 1]).write_bytes(b"PGDMP-clone")
            return _Proc()
        assert Path(argv[-1]).read_bytes() == b"PGDMP-clone"
        _clone_store(primary.engine, spoke.engine)
        return _Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    results = seed.run_seed(client, cfg, report=lambda _m: None)
    assert results["governance"]["watermark"] == 3

    # Post-dump write converges through the SPOKE's gateway.
    _emit(primary, "primary.governance", 1, "decision.recorded")  # seq 4
    with primary.scope() as s:
        emit_mod.deliver_pending(s, client, reachability={})
    assert [e["seq"] for e in spoke.applied] == [4]

    # Every cross-instance URL the seed and the delivery loop dialled went to a
    # PLATFORM host; the plugin hosts were reached only by the gateways.
    cross = [u for u in dialled if "/via/" in u or "/replication" in u]
    assert cross, dialled
    assert all(u.startswith(("http://prim-platform", "http://roam-platform")) for u in dialled), dialled
    assert any(u == "http://prim-platform/via/governance/replication-admin/snapshot" for u in dialled)
    assert any(u == "http://roam-platform/via/governance/events/ingest" for u in dialled)
