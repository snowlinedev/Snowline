"""The #77 stream-contract EMIT side in governance (replication-continuity §4 /
§9 item 3, issue #79): every lifecycle write on the full write surface —
decisions, shadow graph, artifacts, graduation — lands one v2 envelope on the
SDK outbox, in the write's transaction, and the register-class writes keep
their §6 LWW coordinates.

DB-backed (skips cleanly when Postgres is unavailable). The SDK replication
tables ride governance's own alembic chain (the adoption migration), so these
tests also prove the migrated schema carries the SDK models.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from snowline_plugin_sdk.replication import emit as sdk_emit
from snowline_plugin_sdk.replication.models import ReplicationOutboxRow
from sqlalchemy import select

from snowline_governance import artifacts, concurrence, decisions, graduation, shadow
from snowline_governance.contract import (
    CONTRACT_VERSION,
    EVENT_ARTIFACT_MATURITY_SET,
    EVENT_ARTIFACT_REGISTERED,
    EVENT_ARTIFACT_REVISED,
    EVENT_DECISION_RECORDED,
    EVENT_DECISION_SUPERSEDED,
    EVENT_TYPES,
    GOVERNANCE_EVENT_TYPES,
)
from snowline_governance.models import LwwRegister

SOURCE = "primary.governance"
PEER = "roam.governance"


def _sid(slug: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


def _scope_row(slug: str) -> dict:
    return {"id": str(_sid(slug)), "slug": slug}


def _subscribe(session, event_types=None) -> dict:
    """One outbound stream toward the peer, carrying the FULL vocabulary by
    default (source_id passed explicitly — the fail-loud env var stays unset)."""
    return sdk_emit.create_outbound_subscription(
        session,
        "http://roam.example/events/ingest",
        "stream-secret",
        sorted(event_types or EVENT_TYPES),
        epoch="e1",
        source_id=SOURCE,
        peer_source_id=PEER,
    )


def _outbox(session) -> list[ReplicationOutboxRow]:
    return list(
        session.scalars(
            select(ReplicationOutboxRow).order_by(ReplicationOutboxRow.seq)
        )
    )


def test_decision_writes_emit_v2_stream_envelopes(db_session):
    """`record_decision` / `supersede_decision` write outbox rows with the §3.2
    stream contract: emit-time contiguous seq, stream identity, peer_seen, and
    the domain body nested WHOLE under `payload` (v2 — not v1's `decision`)."""
    _subscribe(db_session)
    v1 = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "use postgres", "solid"
    )
    decisions.supersede_decision(db_session, v1["id"], "use postgres 16")

    rows = _outbox(db_session)
    assert [r.event_type for r in rows] == [
        EVENT_DECISION_RECORDED,
        EVENT_DECISION_SUPERSEDED,
    ]
    assert [r.seq for r in rows] == [1, 2]  # emit-time, contiguous
    for row in rows:
        env = row.payload
        assert env["contract_version"] == CONTRACT_VERSION
        assert env["source"] == SOURCE
        assert env["epoch"] == "e1"
        assert env["seq"] == row.seq
        assert env["peer_seen"] == 0  # reverse direction not paired
        body = env["payload"]
        assert body["event_id"] and body["at"]
        assert body["scope"] == "acme/widget"
        assert body["scope_id"] == str(_sid("acme/widget"))
    assert rows[0].payload["payload"]["id"] == v1["id"]
    assert rows[1].payload["payload"]["supersedes_id"] == v1["id"]


def test_full_write_surface_emits_every_registry_event_type(db_session):
    """§4 coverage, pinned against the drift-guarded registry: exercising the
    whole write surface (shadow graph, artifacts, graduation, decisions)
    produces EVERY member of GOVERNANCE_EVENT_TYPES — a new lifecycle write
    can't ship without landing in the registry, and vice versa. Pinned to the
    GOVERNANCE-OWNED subset (#117): EVENT_TYPES is the whole platform's
    vocabulary and includes scope/memory events governance never emits."""
    _subscribe(db_session)
    scope, sid = "acme/widget", _sid("acme/widget")

    # Decisions.
    dec = decisions.record_decision(db_session, scope, sid, "use postgres")
    decisions.supersede_decision(db_session, dec["id"], "use postgres 16")
    # §6.1's explicit compatibility judgment (#97): the verb requires a flagged
    # pair, so flag one (the detection primitive) over two fresh leaves first.
    pa = decisions.record_decision(db_session, scope, sid, "take A")
    pb = decisions.record_decision(db_session, scope, sid, "take B")
    concurrence.flag_pair(
        db_session, uuid.UUID(pa["id"]), uuid.UUID(pb["id"])
    )
    decisions.mark_decisions_compatible(db_session, pa["id"], pb["id"])

    # Shadow graph — every write verb.
    shadow.create_branch(db_session, scope, sid, "line-a", "notes v0")
    shadow.set_narrative_notes(db_session, scope, "line-a", "notes v1")
    node = shadow.add_node(db_session, scope, "line-a", "spec it", "why")
    shadow.add_citation(db_session, node["id"], cited_decision_id=dec["id"])
    shadow.add_message(db_session, uuid.UUID(node["branch_id"]), "hello", "agent")
    # Graduation (the provenance stamp event rides AFTER its decision event).
    graduation.graduate_node(db_session, node["id"])
    shadow.archive_branch(db_session, scope, "line-a")

    # Artifacts (the spec/plan/reference docs) — every write verb.
    art = artifacts.register_artifact(
        db_session,
        body="# spec",
        governs=scope,
        resolved_scopes={scope: _scope_row(scope)},
    )
    v1_id = art["current_version"]["id"]
    artifacts.revise_artifact(db_session, art["id"], "refines", body_snapshot="# v2")
    # A second leaf off the same v1 → competing leaves → resolvable.
    art_after = artifacts.revise_artifact(
        db_session, art["id"], "pivot", supersedes=v1_id, body_snapshot="# alt"
    )
    losing_leaf = art_after["leaves"][0]["id"]
    artifacts.resolve_artifact(db_session, art["id"], losing_leaf)
    artifacts.set_maturity(db_session, art["id"], "stable")
    artifacts.set_governs(
        db_session,
        art["id"],
        ["acme/other"],
        resolved_scopes={"acme/other": _scope_row("acme/other")},
    )

    emitted = {r.event_type for r in _outbox(db_session)}
    assert emitted == set(GOVERNANCE_EVENT_TYPES)
    # …and the stream stayed contiguous through all of it.
    assert [r.seq for r in _outbox(db_session)] == list(
        range(1, len(_outbox(db_session)) + 1)
    )


def test_register_class_writes_record_lww_coordinates(db_session):
    """§6: an in-place write records its (at, source, event_id) coordinate —
    the pure-function input conflict resolution compares on both sides — even
    with NO subscription (authorship state, not delivery state)."""
    art = artifacts.register_artifact(db_session, body="# spec")
    artifacts.set_maturity(db_session, art["id"], "exploratory")

    reg = db_session.get(
        LwwRegister, ("artifact", uuid.UUID(art["id"]), "maturity")
    )
    assert reg is not None
    assert reg.source_id == "governance"  # the lenient local default
    assert reg.event_ref  # the write's event_id
    first_at = reg.written_at

    artifacts.set_maturity(db_session, art["id"], "stable")
    reg = db_session.get(
        LwwRegister, ("artifact", uuid.UUID(art["id"]), "maturity")
    )
    assert reg.written_at >= first_at  # advanced by the newer local write
    # No subscription existed — nothing emitted, register kept regardless.
    assert _outbox(db_session) == []


def test_clock_step_back_clamps_register_writes_monotonic(db_session, monkeypatch):
    """§6 hardening: a same-source wall-clock STEP-BACK (NTP correction) must
    not mint a register-class event that would LOSE to this instance's own
    prior write on the peer — locally the store would hold the new value while
    the peer kept the old one, forever. `emit` clamps the payload `at`
    strictly past the register BEFORE the envelope freezes, so the clamped
    coordinate is what replicates and strict LWW converges on the actual
    latest write everywhere."""
    from snowline_governance import replication_stream

    _subscribe(db_session)
    art = artifacts.register_artifact(db_session, body="# spec")
    artifacts.set_maturity(db_session, art["id"], "exploratory")

    real_now = replication_stream.utcnow()
    monkeypatch.setattr(
        replication_stream, "utcnow", lambda: real_now - timedelta(hours=1)
    )
    artifacts.set_maturity(db_session, art["id"], "stable")

    maturity_ats = [
        replication_stream.parse_at(r.payload["payload"]["at"])
        for r in _outbox(db_session)
        if r.event_type == EVENT_ARTIFACT_MATURITY_SET
    ]
    assert len(maturity_ats) == 2
    assert maturity_ats[1] > maturity_ats[0]  # clamped forward, not stepped back
    reg = db_session.get(
        LwwRegister, ("artifact", uuid.UUID(art["id"]), "maturity")
    )
    assert reg.written_at == maturity_ats[1]  # register tracks the emitted at
    assert artifacts.get_artifact(db_session, art["id"])["maturity"] == "stable"


def test_idempotent_rearchive_emits_nothing(db_session):
    """`archive_branch` re-run is a no-op locally — and emits no second event
    (the emit rides inside the status flip)."""
    _subscribe(db_session)
    scope, sid = "acme/widget", _sid("acme/widget")
    shadow.create_branch(db_session, scope, sid, "line-a")
    shadow.archive_branch(db_session, scope, "line-a")
    n = len(_outbox(db_session))
    shadow.archive_branch(db_session, scope, "line-a")  # idempotent re-archive
    assert len(_outbox(db_session)) == n


def test_milestone_rides_the_artifact_version_payloads(db_session):
    """#141: the SOFT milestone ref MUST travel on the emitted version half of
    both `artifact.registered` and `artifact.revised` — a replicated version
    would silently lose its release stamp otherwise (§4 / §6)."""
    _subscribe(db_session)
    art = artifacts.register_artifact(
        db_session, body="# feature list", milestone="v1-launch"
    )
    artifacts.revise_artifact(
        db_session, art["id"], "refines",
        body_snapshot="# v2", milestone="v2-launch",
    )
    by_type = {r.event_type: r.payload["payload"] for r in _outbox(db_session)}
    assert by_type[EVENT_ARTIFACT_REGISTERED]["version"]["milestone"] == "v1-launch"
    assert by_type[EVENT_ARTIFACT_REVISED]["version"]["milestone"] == "v2-launch"
