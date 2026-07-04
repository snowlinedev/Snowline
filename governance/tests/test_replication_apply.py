"""Governance's replication APPLY half — the §10 acceptance criteria for §6
same-object convergence, §6.1 sibling flagging (including the non-inheriting
negative case), flag clearing, and the retryable-never-skip error posture
(replication-continuity, issue #79).

Structure: direct `apply(session, envelope)` tests for the domain semantics,
plus SDK `ingest_delivery` round-trips where the transaction/gate contract is
itself under test (no-echo, duplicate ACK, 503-then-park-then-reapply), plus
one HTTP pass through the real governance app's ingest route. DB-backed (skips
cleanly without Postgres); the scope service is the conftest stub.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

import pytest
from snowline_plugin_sdk.replication import emit as sdk_emit
from snowline_plugin_sdk.replication import ingest as sdk_ingest
from snowline_plugin_sdk.replication.envelope import build_envelope, sign_body
from snowline_plugin_sdk.replication.models import (
    ReplicationInboundStream,
    ReplicationOutboxRow,
)
from sqlalchemy import select

from snowline_governance import artifacts, concurrence, decisions, graduation, shadow
from snowline_governance.contract import (
    EVENT_ARTIFACT_MATURITY_SET,
    EVENT_DECISION_RECORDED,
    EVENT_DECISION_SUPERSEDED,
    EVENT_SHADOW_BRANCH_ARCHIVED,
    EVENT_SHADOW_CITATION_ADDED,
    EVENT_SHADOW_CONVERSATION_APPENDED,
    EVENT_SHADOW_GRADUATED,
    EVENT_TYPES,
)
from snowline_governance.models import (
    Decision,
    ShadowConversationEvent,
    ShadowNode,
    ShadowNodeCitation,
)
from snowline_governance.replication_apply import build_apply
from snowline_governance.replication_stream import utcnow
from snowline_governance.scope_client import ScopeServiceError

SOURCE = "primary.governance"  # this instance
PEER = "roam.governance"  # the authoring peer
TREE = {"acme": None, "acme/widget": "acme", "acme/other": "acme"}
APPLY_LOG = "snowline.governance.replication_apply"


def _sid(slug: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


def _pair_outbound(session) -> dict:
    """This instance's outbound stream toward the peer — the stream whose
    outbox seqs `peer_seen` counts (§3.2 causal context)."""
    return sdk_emit.create_outbound_subscription(
        session,
        "http://roam.example/events/ingest",
        "stream-secret",
        sorted(EVENT_TYPES),
        epoch="e1",
        source_id=SOURCE,
        peer_source_id=PEER,
    )


def _envelope(event_type: str, payload: dict, *, seq: int = 1, peer_seen: int = 0):
    return build_envelope(
        event_type, payload, source_id=PEER, epoch="pe1", seq=seq,
        peer_seen=peer_seen,
    )


def _payload(**fields) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "at": utcnow().isoformat(),
        **fields,
    }


def _decision_payload(
    scope: str = "acme/widget",
    decision: str = "peer decision",
    *,
    decision_id: str | None = None,
    supersedes_id: str | None = None,
) -> dict:
    return _payload(
        id=decision_id or str(uuid.uuid4()),
        scope_id=str(_sid(scope)),
        scope=scope,
        decision=decision,
        rationale=None,
        recorded_at=utcnow().isoformat(),
        supersedes_id=supersedes_id,
    )


@pytest.fixture()
def apply_fn(stub_scope_client):
    return build_apply(stub_scope_client(TREE))


# --- idempotent apply, identity preserved -------------------------------------


def test_decision_apply_preserves_identity_and_is_idempotent(db_session, apply_fn):
    p = _decision_payload(decision="use postgres")
    env = _envelope(EVENT_DECISION_RECORDED, p)
    apply_fn(db_session, env)
    apply_fn(db_session, env)  # §4 checklist 4: replay is a no-op

    rows = list(db_session.scalars(select(Decision)))
    assert len(rows) == 1
    assert str(rows[0].id) == p["id"]  # authoring UUID preserved
    assert rows[0].scope_slug == "acme/widget"
    assert rows[0].decision == "use postgres"


def test_losing_superseded_event_is_applied_never_skipped(db_session, apply_fn):
    """§6: 'X superseded by A here and by B there' takes BOTH paths — B's
    append half survives as a second superseding leaf (branching DAG; the
    store converges mechanically), AND §6.1 flags A-vs-B for review."""
    _pair_outbound(db_session)
    x = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "X"
    )
    a = decisions.supersede_decision(db_session, x["id"], "A supersedes X")

    b_payload = _decision_payload(decision="B supersedes X", supersedes_id=x["id"])
    apply_fn(
        db_session, _envelope(EVENT_DECISION_SUPERSEDED, b_payload, peer_seen=0)
    )

    b_row = db_session.get(Decision, uuid.UUID(b_payload["id"]))
    assert b_row is not None  # applied, never skipped
    assert str(b_row.supersedes_id) == x["id"]
    # Both superseders stand as leaves — and the pair is flagged (§6.1).
    assert concurrence.concurrent_with(db_session, uuid.UUID(a["id"])) == [
        b_payload["id"]
    ]


# --- §6.1 concurrent-sibling detection -----------------------------------------


def test_siblings_flagged_in_same_scope(db_session, apply_fn):
    _pair_outbound(db_session)
    local = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "local take"
    )
    incoming = _decision_payload(scope="acme/widget", decision="peer take")
    apply_fn(db_session, _envelope(EVENT_DECISION_RECORDED, incoming, peer_seen=0))

    view = concurrence.unreconciled_pairs(db_session)
    assert view["items_total"] == 1
    flagged = {d["id"] for d in view["pairs"][0]["decisions"]}
    assert flagged == {local["id"], incoming["id"]}
    # Markers surface on BOTH decisions' full reads.
    assert decisions.get_decision(db_session, local["id"])["concurrent_with"] == [
        incoming["id"]
    ]
    assert decisions.get_decision(db_session, incoming["id"])[
        "concurrent_with"
    ] == [local["id"]]


def test_siblings_flagged_along_the_applicability_chain_both_directions(
    db_session, apply_fn
):
    """A parent-scope decision governs descendants, so the collision surface is
    the ancestors-until-isolated chain in EITHER direction."""
    _pair_outbound(db_session)
    # Local decision at the PARENT scope; incoming at the child.
    parent = decisions.record_decision(db_session, "acme", _sid("acme"), "org rule")
    child_in = _decision_payload(scope="acme/widget")
    apply_fn(db_session, _envelope(EVENT_DECISION_RECORDED, child_in, peer_seen=0))
    assert concurrence.concurrent_with(db_session, uuid.UUID(parent["id"])) == [
        child_in["id"]
    ]

    # Local decision at the CHILD scope; incoming at the parent (seq 2).
    child_local = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "widget rule"
    )
    parent_in = _decision_payload(scope="acme")
    apply_fn(
        db_session,
        _envelope(EVENT_DECISION_RECORDED, parent_in, seq=2, peer_seen=0),
    )
    assert parent_in["id"] in concurrence.concurrent_with(
        db_session, uuid.UUID(child_local["id"])
    )


def test_non_inheriting_sibling_scopes_are_not_flagged(db_session, apply_fn):
    """The §10 NEGATIVE case: distinct scopes that merely share an ancestor
    (`acme/widget` vs `acme/other`) do not inherit each other's governance —
    concurrent decisions there are NOT flagged."""
    _pair_outbound(db_session)
    decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "widget take"
    )
    incoming = _decision_payload(scope="acme/other")
    apply_fn(db_session, _envelope(EVENT_DECISION_RECORDED, incoming, peer_seen=0))
    assert concurrence.unreconciled_pairs(db_session)["items_total"] == 0


def test_isolated_scope_blocks_chain_collision(db_session, stub_scope_client):
    """An `isolated` child halts inheritance from above — a concurrent pair
    (parent scope vs isolated child) does not collide."""
    apply_fn = build_apply(stub_scope_client(TREE, isolated={"acme/widget"}))
    _pair_outbound(db_session)
    decisions.record_decision(db_session, "acme", _sid("acme"), "org rule")
    incoming = _decision_payload(scope="acme/widget")
    apply_fn(db_session, _envelope(EVENT_DECISION_RECORDED, incoming, peer_seen=0))
    assert concurrence.unreconciled_pairs(db_session)["items_total"] == 0


def test_peer_seen_makes_sequential_decisions_unflagged(db_session, apply_fn):
    """Causality, not clocks: a peer decision authored AFTER applying my seq 1
    (peer_seen=1) is sequential with it — same scope, no flag."""
    _pair_outbound(db_session)
    decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "local take"
    )
    incoming = _decision_payload(scope="acme/widget")
    apply_fn(db_session, _envelope(EVENT_DECISION_RECORDED, incoming, peer_seen=1))
    assert concurrence.unreconciled_pairs(db_session)["items_total"] == 0


def test_flag_clears_via_local_supersession(db_session, apply_fn):
    """Reconciliation is ordinary governance: superseding either member makes
    the pair reconciled — derived state, no marker write."""
    _pair_outbound(db_session)
    local = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "local take"
    )
    incoming = _decision_payload(scope="acme/widget")
    apply_fn(db_session, _envelope(EVENT_DECISION_RECORDED, incoming, peer_seen=0))
    assert concurrence.unreconciled_pairs(db_session)["items_total"] == 1

    decisions.supersede_decision(
        db_session, incoming["id"], "merged: local take stands"
    )
    assert concurrence.unreconciled_pairs(db_session)["items_total"] == 0
    assert (
        decisions.get_decision(db_session, local["id"])["concurrent_with"] == []
    )


def test_flag_clears_when_the_supersession_arrives_as_an_event(
    db_session, apply_fn
):
    """The OTHER side of §10's flag clearing: the reconciling supersession is a
    normal event — applying it clears the flag here too (both sides converge
    from events alone)."""
    _pair_outbound(db_session)
    decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "local take"
    )
    incoming = _decision_payload(scope="acme/widget")
    apply_fn(db_session, _envelope(EVENT_DECISION_RECORDED, incoming, peer_seen=0))
    assert concurrence.unreconciled_pairs(db_session)["items_total"] == 1

    # The peer reconciles after heal (it has applied my seq 1 → peer_seen=1),
    # superseding its own member of the pair.
    fix = _decision_payload(
        scope="acme/widget",
        decision="merged: local take stands",
        supersedes_id=incoming["id"],
    )
    apply_fn(
        db_session, _envelope(EVENT_DECISION_SUPERSEDED, fix, seq=2, peer_seen=1)
    )
    assert concurrence.unreconciled_pairs(db_session)["items_total"] == 0


# --- retryable errors: never a silent skip --------------------------------------


class _OutageScopeClient:
    def resolve(self, slug: str):
        raise ScopeServiceError("platform scope service unreachable (test)")

    def ancestors(self, slug: str):
        raise ScopeServiceError("platform scope service unreachable (test)")


def _ingest(session, apply_fn, envelope: dict, secret: str, park_after=None):
    body = json.dumps(envelope).encode()
    return sdk_ingest.ingest_delivery(
        session,
        body,
        f"sha256={sign_body(secret, body)}",
        apply_fn,
        park_after=park_after,
    )


def test_scope_outage_is_retryable_then_parks_then_reapplies(
    migrated_db, clean_db, stub_scope_client
):
    """§6.1/§8.1: a scope-service outage during detection is a BOUNDED
    RETRYABLE apply error — 503s (nothing applied, gate pinned), parks loudly
    at the bound (gate advances, applied frontier pins), and the parked event
    re-applies successfully once the platform is back — never a silent skip of
    detection."""
    from snowline_governance.db import session_scope

    with session_scope() as s:
        _pair_outbound(s)
        decisions.record_decision(
            s, "acme/widget", _sid("acme/widget"), "local take"
        )
        secret = sdk_ingest.register_inbound_stream(s, PEER, "pe1")["secret"]

    incoming = _decision_payload(scope="acme/widget")
    envelope = _envelope(EVENT_DECISION_RECORDED, incoming, peer_seen=0)
    broken = build_apply(_OutageScopeClient())

    # Attempt 1: retryable (503) — the failed apply rolled back, gate pinned.
    with session_scope() as s:
        status, body = _ingest(s, broken, envelope, secret, park_after=2)
        assert (status, body["status"]) == (503, "retry")
    with session_scope() as s:
        assert s.get(Decision, uuid.UUID(incoming["id"])) is None
        stream = s.get(ReplicationInboundStream, (PEER, "pe1"))
        assert (stream.gate_seq, stream.applied_seq) == (0, 0)

    # Attempt 2 hits the bound: PARKED — ACKs, gate advances, frontier pins.
    with session_scope() as s:
        status, body = _ingest(s, broken, envelope, secret, park_after=2)
        assert (status, body["status"]) == (200, "parked")
    with session_scope() as s:
        stream = s.get(ReplicationInboundStream, (PEER, "pe1"))
        assert (stream.gate_seq, stream.applied_seq) == (1, 0)
        assert s.get(Decision, uuid.UUID(incoming["id"])) is None

    # Cause fixed (platform back): re-apply from the park — the decision lands
    # AND detection runs (the flag appears), frontier unpins.
    working = build_apply(stub_scope_client(TREE))
    with session_scope() as s:
        sdk_ingest.reapply_parked(s, PEER, "pe1", 1, working)
    with session_scope() as s:
        assert s.get(Decision, uuid.UUID(incoming["id"])) is not None
        assert concurrence.unreconciled_pairs(s)["items_total"] == 1
        stream = s.get(ReplicationInboundStream, (PEER, "pe1"))
        assert stream.applied_seq == 1


def test_unknown_scope_slug_is_retryable(db_session, apply_fn):
    """§8: a branch in a scope this instance doesn't know yet raises (ordinary
    scope-stream lag self-heals on redelivery) — never half-applies."""
    payload = _payload(
        id=str(uuid.uuid4()),
        scope_id=str(uuid.uuid4()),
        scope="acme/unknown",
        name="line-x",
        narrative_notes=None,
        created_at=utcnow().isoformat(),
    )
    with pytest.raises(ValueError, match="unknown scope slug"):
        apply_fn(db_session, _envelope("shadow.branch_created", payload))


def test_unknown_event_type_is_retryable(db_session, apply_fn):
    with pytest.raises(ValueError, match="unknown replicated event type"):
        apply_fn(db_session, _envelope("governance.future_thing", _payload()))


# --- SDK round-trip: no echo, duplicate ACK, HTTP route ---------------------------


def test_ingest_roundtrip_no_echo_and_duplicate_ack(migrated_db, clean_db, stub_scope_client):
    """§10: an applied event never re-emits (the paired outbound stream's
    outbox stays quiet), and redelivery is a watermark duplicate ACK applied
    exactly once."""
    from snowline_governance.db import session_scope

    with session_scope() as s:
        _pair_outbound(s)  # a live outbound stream an echo WOULD land on
        secret = sdk_ingest.register_inbound_stream(s, PEER, "pe1")["secret"]

    apply_fn = build_apply(stub_scope_client(TREE))
    incoming = _decision_payload()
    envelope = _envelope(EVENT_DECISION_RECORDED, incoming)

    with session_scope() as s:
        status, body = _ingest(s, apply_fn, envelope, secret)
        assert (status, body["status"]) == (200, "applied")
    with session_scope() as s:
        assert s.get(Decision, uuid.UUID(incoming["id"])) is not None
        assert list(s.scalars(select(ReplicationOutboxRow))) == []  # NO ECHO

    with session_scope() as s:  # redelivery: duplicate no-op ACK
        status, body = _ingest(s, apply_fn, envelope, secret)
        assert (status, body["status"]) == (200, "duplicate")
    with session_scope() as s:
        assert len(list(s.scalars(select(Decision)))) == 1


def test_http_ingest_through_the_governance_app(migrated_db, clean_db, stub_scope_client, monkeypatch):
    """The wired surface: the SDK router in `create_app` serves the manifest's
    `/events/ingest` over governance's own session_scope — one signed delivery
    lands, from a trusted (loopback) peer."""
    import anyio
    import httpx

    from snowline_governance.app import create_app
    from snowline_governance.db import session_scope

    monkeypatch.setenv("SNOWLINE_WEBHOOK_DISABLED", "1")
    app = create_app(
        scope_client=stub_scope_client(TREE),
        migrate_on_startup=False,
        register_on_startup=False,
    )
    with session_scope() as s:
        secret = sdk_ingest.register_inbound_stream(s, PEER, "pe1")["secret"]

    incoming = _decision_payload()
    body = json.dumps(_envelope(EVENT_DECISION_RECORDED, incoming)).encode()

    async def deliver() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 4242))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gov"
        ) as client:
            return await client.post(
                "/events/ingest",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Snowline-Signature": f"sha256={sign_body(secret, body)}",
                },
            )

    resp = anyio.run(deliver)
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    with session_scope() as s:
        assert s.get(Decision, uuid.UUID(incoming["id"])) is not None


# --- §6 same-object LWW ----------------------------------------------------------


def test_lww_incoming_older_write_loses_and_is_logged(db_session, apply_fn, caplog):
    """§10 same-object convergence, this side's half: the local write is newer
    → the incoming (older) mutation yields, at WARNING with both event ids."""
    art = artifacts.register_artifact(db_session, body="# spec")
    artifacts.set_maturity(db_session, art["id"], "stable")  # local, newer

    older = _payload(artifact_id=art["id"], maturity="draft")
    older["at"] = (utcnow() - timedelta(minutes=5)).isoformat()
    with caplog.at_level(logging.WARNING, logger=APPLY_LOG):
        apply_fn(db_session, _envelope(EVENT_ARTIFACT_MATURITY_SET, older))

    assert artifacts.get_artifact(db_session, art["id"])["maturity"] == "stable"
    conflict_logs = [r for r in caplog.records if "resolved by LWW" in r.message]
    assert len(conflict_logs) == 1
    logged = conflict_logs[0].getMessage()
    assert older["event_id"] in logged  # the losing event id…
    # …and the winning (register) event id — the local set_maturity write.
    from snowline_governance.models import LwwRegister

    reg = db_session.get(LwwRegister, ("artifact", uuid.UUID(art["id"]), "maturity"))
    assert reg.event_ref in logged


def test_lww_incoming_newer_write_wins(db_session, apply_fn):
    art = artifacts.register_artifact(db_session, body="# spec")
    artifacts.set_maturity(db_session, art["id"], "exploratory")

    newer = _payload(artifact_id=art["id"], maturity="stable")
    newer["at"] = (utcnow() + timedelta(minutes=5)).isoformat()
    apply_fn(db_session, _envelope(EVENT_ARTIFACT_MATURITY_SET, newer))
    assert artifacts.get_artifact(db_session, art["id"])["maturity"] == "stable"


def test_lww_is_order_independent(db_session, apply_fn):
    """The §6 pure-function property: two conflicting register writes converge
    to the SAME winner whichever order they apply in (two fresh objects, same
    two coordinates, opposite orders)."""
    t1 = (utcnow() - timedelta(minutes=10)).isoformat()
    t2 = (utcnow() - timedelta(minutes=5)).isoformat()

    def events_for(artifact_id: str):
        older = _payload(artifact_id=artifact_id, maturity="draft")
        older["at"] = t1
        newer = _payload(artifact_id=artifact_id, maturity="stable")
        newer["at"] = t2
        return older, newer

    a1 = artifacts.register_artifact(db_session, body="# a1")
    older, newer = events_for(a1["id"])
    apply_fn(db_session, _envelope(EVENT_ARTIFACT_MATURITY_SET, older, seq=1))
    apply_fn(db_session, _envelope(EVENT_ARTIFACT_MATURITY_SET, newer, seq=2))

    a2 = artifacts.register_artifact(db_session, body="# a2")
    older, newer = events_for(a2["id"])
    apply_fn(db_session, _envelope(EVENT_ARTIFACT_MATURITY_SET, newer, seq=3))
    apply_fn(db_session, _envelope(EVENT_ARTIFACT_MATURITY_SET, older, seq=4))

    assert artifacts.get_artifact(db_session, a1["id"])["maturity"] == "stable"
    assert artifacts.get_artifact(db_session, a2["id"])["maturity"] == "stable"


def test_lww_source_id_tiebreak_is_deterministic(db_session, apply_fn):
    """Equal timestamps (a skewed race): the `source_id` tiebreak still picks
    one winner — the same one on both sides."""
    art = artifacts.register_artifact(db_session, body="# spec")
    at = utcnow().isoformat()
    # The local register write is from "governance"; PEER ("roam.governance")
    # sorts HIGHER, so at an exactly-equal timestamp the peer's write wins.
    from snowline_governance.models import LwwRegister
    from snowline_governance.replication_stream import parse_at, record_register

    record_register(
        db_session, "artifact", uuid.UUID(art["id"]), "maturity",
        at=parse_at(at), source="governance", event_ref=str(uuid.uuid4()),
    )
    incoming = _payload(artifact_id=art["id"], maturity="stable")
    incoming["at"] = at
    apply_fn(db_session, _envelope(EVENT_ARTIFACT_MATURITY_SET, incoming))
    assert artifacts.get_artifact(db_session, art["id"])["maturity"] == "stable"
    reg = db_session.get(LwwRegister, ("artifact", uuid.UUID(art["id"]), "maturity"))
    assert reg.source_id == PEER


def test_concurrent_double_graduation_resolves_by_lww_append_survives(
    db_session, apply_fn, caplog
):
    """Both sides graduate the SAME node across a partition: the node's
    pointer resolves by LWW, and the losing side's graduated decision is
    applied-then-overridden — its row and provenance stamps survive; only the
    contested pointer yields. WARNING carries both event ids."""
    _pair_outbound(db_session)
    scope, sid = "acme/widget", _sid("acme/widget")
    shadow.create_branch(db_session, scope, sid, "line-a")
    node = shadow.add_node(db_session, scope, "line-a", "spec it")
    grad = graduation.graduate_node(db_session, node["id"])  # local, newer

    # The peer's competing graduation, authored EARLIER in the partition:
    # its decision event first (append), then its stamp (the contested write).
    peer_decision = _decision_payload(decision="peer graduation")
    peer_decision["at"] = (utcnow() - timedelta(minutes=5)).isoformat()
    apply_fn(
        db_session, _envelope(EVENT_DECISION_RECORDED, peer_decision, seq=1)
    )
    stamp = _payload(
        decision_id=peer_decision["id"],
        node_id=node["id"],
        label=f"{scope}:line-a",
        kind=None,
    )
    stamp["at"] = (utcnow() - timedelta(minutes=5)).isoformat()
    with caplog.at_level(logging.WARNING, logger=APPLY_LOG):
        apply_fn(db_session, _envelope(EVENT_SHADOW_GRADUATED, stamp, seq=2))

    # The contested pointer kept the newer (local) graduation…
    node_row = db_session.get(ShadowNode, uuid.UUID(node["id"]))
    assert str(node_row.graduated_decision_id) == grad["decision_id"]
    # …but the peer's decision survived WITH its provenance stamps (the
    # applied-then-overridden append half).
    peer_row = db_session.get(Decision, uuid.UUID(peer_decision["id"]))
    assert peer_row.shadow_origin_node_id == node["id"]
    assert peer_row.shadow_origin_label == f"{scope}:line-a"
    assert any("resolved by LWW" in r.message for r in caplog.records)


# --- the rest of the write surface: convergence semantics -------------------------


def test_conversation_apply_reallocates_seq_and_bypasses_archive_guard(
    db_session, apply_fn
):
    """A replicated message lands with a locally-allocated seq (the set
    converges; seq is presentation-local), is idempotent by id, and is applied
    even on an archived branch — authored before its author saw the archive,
    applied, never skipped."""
    scope, sid = "acme/widget", _sid("acme/widget")
    branch = shadow.create_branch(db_session, scope, sid, "line-a")
    shadow.add_message(db_session, branch["id"], "local one", "agent")
    shadow.archive_branch(db_session, scope, "line-a")

    incoming = _payload(
        id=str(uuid.uuid4()),
        branch_id=branch["id"],
        kind="message",
        payload={"author": "human", "markdown": "typed on the spoke"},
        created_at=utcnow().isoformat(),
    )
    env = _envelope(EVENT_SHADOW_CONVERSATION_APPENDED, incoming)
    apply_fn(db_session, env)
    apply_fn(db_session, env)  # idempotent

    rows = list(
        db_session.scalars(
            select(ShadowConversationEvent).order_by(ShadowConversationEvent.seq)
        )
    )
    assert [r.seq for r in rows] == [1, 2]  # re-allocated after the local one
    assert str(rows[-1].id) == incoming["id"]


def test_branch_archive_converges_to_the_earliest_archival(db_session, apply_fn):
    scope, sid = "acme/widget", _sid("acme/widget")
    branch = shadow.create_branch(db_session, scope, sid, "line-a")
    archived = shadow.archive_branch(db_session, scope, "line-a")

    earlier = (utcnow() - timedelta(hours=1)).replace(microsecond=0)
    incoming = _payload(branch_id=branch["id"], archived_at=earlier.isoformat())
    apply_fn(db_session, _envelope(EVENT_SHADOW_BRANCH_ARCHIVED, incoming))
    got = shadow.get_branch(db_session, scope, "line-a")
    assert got["archived_at"] == earlier.isoformat()  # min() — deterministic
    assert got["archived_at"] < archived["archived_at"]

    later = _payload(
        branch_id=branch["id"],
        archived_at=(utcnow() + timedelta(hours=1)).isoformat(),
    )
    apply_fn(db_session, _envelope(EVENT_SHADOW_BRANCH_ARCHIVED, later))
    assert (
        shadow.get_branch(db_session, scope, "line-a")["archived_at"]
        == earlier.isoformat()
    )


def test_citation_apply_dedupes_on_the_edge(db_session, apply_fn):
    """The same (node → target) edge authored on both sides converges on ONE
    edge (the row id is display-only); a distinct edge still applies."""
    scope, sid = "acme/widget", _sid("acme/widget")
    dec = decisions.record_decision(db_session, scope, sid, "use postgres")
    dec2 = decisions.record_decision(db_session, scope, sid, "use uv")
    shadow.create_branch(db_session, scope, sid, "line-a")
    node = shadow.add_node(db_session, scope, "line-a", "spec it")
    shadow.add_citation(db_session, node["id"], cited_decision_id=dec["id"])

    same_edge = _payload(
        id=str(uuid.uuid4()),
        node_id=node["id"],
        cited_node_id=None,
        cited_decision_id=dec["id"],
        created_at=utcnow().isoformat(),
    )
    apply_fn(db_session, _envelope(EVENT_SHADOW_CITATION_ADDED, same_edge))
    assert len(shadow.list_citations(db_session, node["id"])) == 1

    other_edge = _payload(
        id=str(uuid.uuid4()),
        node_id=node["id"],
        cited_node_id=None,
        cited_decision_id=dec2["id"],
        created_at=utcnow().isoformat(),
    )
    apply_fn(db_session, _envelope(EVENT_SHADOW_CITATION_ADDED, other_edge, seq=2))
    citations = shadow.list_citations(db_session, node["id"])
    assert len(citations) == 2
    assert str(
        db_session.get(ShadowNodeCitation, uuid.UUID(other_edge["id"])).id
    ) == other_edge["id"]  # a NEW edge keeps its authoring id
