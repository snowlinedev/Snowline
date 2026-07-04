"""`snowline replicate seed` (replication-continuity §7/§10, issue #82): the seed
library exercised without a live Postgres. `pg_dump`/`pg_restore` (step 2) are
covered by the real-Postgres drill; here the dump is SIMULATED by cloning the
primary's store into the spoke (what a restore produces), so the load-bearing
logic — the priming, the §7-step-3 scrub-then-inject, the gapless
snapshot-to-stream handoff, and both re-seed preconditions — is unit-tested on
SQLite.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from snowline_platform import replication_seed as seed
from snowline_platform.replication_pairing import Participant
from snowline_plugin_sdk.replication import emit as emit_mod
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
        primary_dump_url="postgresql+psycopg:///unused_in_this_test",
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
    with pytest.raises(seed.SeedError, match="undelivered outbox"):
        seed.check_reseed_preconditions(client, cfg, report=lambda _m: None)


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
                "primary_dump_url": "postgresql:///gov_primary",
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
    # The primary plugin's loopback base_url is rewritten onto the primary's
    # tailnet host (derived from platform_url), port preserved (§4.1).
    assert gov.primary.admin_base == "http://prim-platform:8801/replication-admin"
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
            "primary_dump_url": "x", "spoke_db_url": "y",
        }},
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(config))
    with pytest.raises(seed.SeedError, match="does not declare replication"):
        seed.load_seed_config(client, path)


def test_libpq_and_sqlalchemy_url_normalization():
    assert seed._libpq_url("postgresql+psycopg:///db") == "postgresql:///db"
    assert seed._libpq_url("postgresql:///db") == "postgresql:///db"
    assert seed._sqlalchemy_url("postgresql:///db") == "postgresql+psycopg:///db"
    assert seed._sqlalchemy_url("sqlite:///x.db") == "sqlite:///x.db"


# --- helpers ------------------------------------------------------------------


def _emit(inst, source_id, n, event_type, monkeypatch=None):
    import os

    os.environ["SNOWLINE_REPLICATION_SOURCE_ID"] = source_id
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
