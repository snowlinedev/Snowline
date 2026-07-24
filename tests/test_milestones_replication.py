"""The milestone registry adopting the replication-class contract (milestones.md
§9, issue #145) — the pattern scopes established (`test_scopes_replication.py`),
raised to a lifecycle state machine plus a mutable DAG, so the §6 LWW rules are
exercised explicitly:

  * EVERY mutating verb emits its event with FULL ROW STATE + an authored-at stamp
    (a verb with no event silently never replicates).
  * Round-trip apply is ADDRESS-KEYED: the anchor `Scope` is re-resolved by SLUG
    at apply (the wire carries no instance-local UUID), landing on the local row.
  * Concurrent activate/cancel converges by LWW (§6): the loser's transition still
    lands in the log, and a converged history illegal under §4 (cancelled→active)
    is FIRST-CLASS unreconciled state — apply never parks on mere LWW loss.
  * A dependency add / a merge whose UNION cycles the local DAG or alias graph is
    REJECTED AND PARKED (§8.1), keeping the walks loop-free by construction.

Envelopes are hand-built (mirroring the scope test): one Postgres database plays
"this instance's store" for the apply side, which is what `apply_milestone_event`
reads/writes against in production. The anchor scopes a peer's milestones
reference are created LOCALLY first — standing in for the scope stream having
already replicated them (the ordering guarantee the platform stream provides).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from snowline_platform import milestones, replication, scopes
from snowline_platform.models import MilestoneDependency
from snowline_plugin_sdk.contract import (
    EVENT_MILESTONE_CREATED,
    EVENT_MILESTONE_DEPENDENCY_CHANGED,
    EVENT_MILESTONE_MERGED,
    EVENT_MILESTONE_TRANSITIONED,
    EVENT_MILESTONE_UPDATED,
)
from snowline_plugin_sdk.replication import emit, ingest
from snowline_plugin_sdk.replication.envelope import build_envelope, sign_body
from snowline_plugin_sdk.replication.models import (
    ReplicationInboundStream,
    ReplicationOutboxRow,
)

STREAM = ("peer.platform", "epoch-1")
T0 = datetime(2026, 7, 20, 12, 0, 0)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _register(session):
    out = ingest.register_inbound_stream(session, *STREAM)
    session.commit()
    return out


def _anchor(session, org="acme", repo="repo"):
    """Create the anchor scope tree LOCALLY — standing in for the scope stream
    having already replicated it (apply re-resolves the anchor by slug)."""
    scopes.create(session, slug=org, name=org.title(), kind="org")
    scopes.create(session, slug=f"{org}/{repo}", name=repo.title(), kind="project", parent=org)
    session.commit()


def _m_payload(anchor, name, *, outcome=None, status="planned", authored_at,
               target_date=None, activated_at=None, achieved_at=None,
               cancelled_at=None, merged_into=None):
    return {
        "address": f"{anchor}/{name}",
        "anchor": anchor,
        "name": name,
        "outcome": outcome,
        "status": status,
        "target_date": target_date,
        "activated_at": _iso(activated_at),
        "achieved_at": _iso(achieved_at),
        "cancelled_at": _iso(cancelled_at),
        "merged_into": merged_into,
        "authored_at": _iso(authored_at),
    }


def _transitioned(anchor, name, *, from_status, to_status, authored_at,
                  reason=None, **row):
    return {
        **_m_payload(anchor, name, status=to_status, authored_at=authored_at, **row),
        "from_status": from_status,
        "to_status": to_status,
        "reason": reason,
    }


def _deliver(session, secret, event_type, payload, seq, **kw):
    """One delivery per transaction (the ingest TRANSACTION CONTRACT). Driven
    through the PLATFORM dispatcher (`apply_platform_event`), exercising the
    scope-vs-milestone routing exactly as production does."""
    envelope = build_envelope(
        event_type, payload, source_id=STREAM[0], epoch=STREAM[1], seq=seq, peer_seen=0
    )
    body = json.dumps(envelope).encode()
    sig = f"sha256={sign_body(secret, body)}"
    out = ingest.ingest_delivery(
        session, body, sig, replication.apply_platform_event, **kw
    )
    session.commit()
    return out


def _stream(session):
    return session.get(ReplicationInboundStream, STREAM)


# --- emit side: every mutating verb writes its event with full row state ------


def test_every_verb_emits_full_row_state_and_authored_at(db_session):
    """§9: create / update / transition / dependency edit / merge each emit their
    event carrying FULL ROW STATE + an authored-at stamp — a verb with no event
    silently never replicates."""
    emit.create_outbound_subscription(
        db_session,
        "http://peer/replication/events/ingest",
        "topsecret",
        list(replication.MILESTONE_EVENTS),
        epoch="e1",
        source_id="hub.platform",
    )
    scopes.create(db_session, slug="acme", name="Acme", kind="org")
    scopes.create(db_session, slug="acme/repo", name="Repo", kind="project", parent="acme")
    db_session.commit()

    a = milestones.create(db_session, "acme/repo", "alpha", outcome="ship it")
    b = milestones.create(db_session, "acme/repo", "beta")
    db_session.commit()
    milestones.update(db_session, "acme/repo/alpha", outcome="ship it well")
    milestones.activate(db_session, "acme/repo/alpha", reason="go")
    milestones.add_dependency(db_session, "acme/repo/beta", "acme/repo/alpha")
    db_session.commit()
    milestones.merge(db_session, "acme/repo/beta", "acme/repo/alpha")
    db_session.commit()

    rows = db_session.scalars(
        select(ReplicationOutboxRow).order_by(ReplicationOutboxRow.seq)
    ).all()
    kinds = [r.payload["event_type"] for r in rows]
    assert kinds == [
        EVENT_MILESTONE_CREATED,   # alpha
        EVENT_MILESTONE_CREATED,   # beta
        EVENT_MILESTONE_UPDATED,   # alpha outcome
        EVENT_MILESTONE_TRANSITIONED,  # alpha activate
        EVENT_MILESTONE_DEPENDENCY_CHANGED,  # beta -> alpha
        EVENT_MILESTONE_MERGED,    # beta into alpha
    ]
    # Full row state + authored_at on the created event (address-keyed, no UUID).
    created = rows[0].payload["payload"]
    assert created["address"] == "acme/repo/alpha"
    assert created["anchor"] == "acme/repo"
    assert created["name"] == "alpha"
    assert created["outcome"] == "ship it"
    assert created["status"] == "planned"
    assert created["authored_at"] is not None
    assert "id" not in created  # cross-instance identity is the address, not the UUID

    # The transition event carries the from/to/reason triple + full row state.
    trans = rows[3].payload["payload"]
    assert (trans["from_status"], trans["to_status"], trans["reason"]) == (
        "planned", "active", "go"
    )
    assert trans["status"] == "active" and trans["activated_at"] is not None

    # The dependency event is an add/remove DELTA (documented choice, §9).
    dep = rows[4].payload["payload"]
    assert dep == {
        "op": "add",
        "dependent": "acme/repo/beta",
        "dependency": "acme/repo/alpha",
        "authored_at": dep["authored_at"],
    }
    # The merged event carries the tombstone + terminal-target addresses.
    merged = rows[5].payload["payload"]
    assert merged["from"] == "acme/repo/beta" and merged["into"] == "acme/repo/alpha"


def test_no_subscription_emits_nothing(db_session):
    """No outbound stream (pre-pairing default) — emitting is a harmless no-op."""
    scopes.create(db_session, slug="acme", name="Acme", kind="org")
    db_session.commit()
    milestones.create(db_session, "acme", "alpha")
    db_session.commit()
    assert db_session.scalars(select(ReplicationOutboxRow)).all() == []


def test_noop_update_does_not_stamp_or_emit(db_session):
    """A content-free `update` (nothing provided, or values equal to stored)
    advances NO LWW clock and emits NOTHING — it must not be able to shadow a
    genuine concurrent peer update (review finding on §9)."""
    emit.create_outbound_subscription(
        db_session,
        "http://peer/replication/events/ingest",
        "topsecret",
        list(replication.MILESTONE_EVENTS),
        epoch="e1",
        source_id="hub.platform",
    )
    scopes.create(db_session, slug="acme", name="Acme", kind="org")
    db_session.commit()
    milestones.create(db_session, "acme", "alpha", outcome="ship")
    db_session.commit()
    before_clock = milestones.get(db_session, "acme/alpha").lww_authored_at
    before_outbox = len(db_session.scalars(select(ReplicationOutboxRow)).all())

    milestones.update(db_session, "acme/alpha")  # nothing provided
    milestones.update(db_session, "acme/alpha", outcome="ship")  # equal value
    db_session.commit()

    m = milestones.get(db_session, "acme/alpha")
    assert m.lww_authored_at == before_clock
    assert len(db_session.scalars(select(ReplicationOutboxRow)).all()) == before_outbox

    # A REAL change still stamps + emits.
    milestones.update(db_session, "acme/alpha", outcome="ship v1")
    db_session.commit()
    m = milestones.get(db_session, "acme/alpha")
    assert m.lww_authored_at != before_clock
    assert (
        len(db_session.scalars(select(ReplicationOutboxRow)).all())
        == before_outbox + 1
    )


# --- round-trip apply: address-keyed, anchor re-resolved by slug --------------


def test_round_trip_apply_converges_address_keyed_with_anchor_re_resolved(db_session):
    """A peer's milestone applies onto a clean store keyed by its CANONICAL
    ADDRESS, with `anchor_scope_id` re-resolved from the anchor slug at apply — it
    lands on the LOCAL anchor row, never a foreign UUID (§9)."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]
    local_anchor = scopes.resolve(db_session, "acme/repo")

    status, resp = _deliver(
        db_session, secret, EVENT_MILESTONE_CREATED,
        _m_payload("acme/repo", "v1-launch", outcome="GA", authored_at=T0), 1,
    )
    assert (status, resp["status"]) == (200, "applied")
    m = milestones.get(db_session, "acme/repo/v1-launch")
    assert m.status == "planned" and m.outcome == "GA"
    assert m.anchor_scope_id == local_anchor.id  # re-resolved by slug, not a wire UUID

    # updated converges the mutable fields.
    status, resp = _deliver(
        db_session, secret, EVENT_MILESTONE_UPDATED,
        _m_payload("acme/repo", "v1-launch", outcome="GA (revised)",
                   authored_at=T0 + timedelta(minutes=1)), 2,
    )
    assert (status, resp["status"]) == (200, "applied")
    assert milestones.get(db_session, "acme/repo/v1-launch").outcome == "GA (revised)"

    # transitioned converges status AND lands the transition in the log.
    status, resp = _deliver(
        db_session, secret, EVENT_MILESTONE_TRANSITIONED,
        _transitioned("acme/repo", "v1-launch", from_status="planned",
                      to_status="active", authored_at=T0 + timedelta(minutes=2),
                      activated_at=T0 + timedelta(minutes=2)), 3,
    )
    assert (status, resp["status"]) == (200, "applied")
    assert milestones.get(db_session, "acme/repo/v1-launch").status == "active"
    log = milestones.transitions(db_session, "acme/repo/v1-launch")
    assert [(t["from_status"], t["to_status"]) for t in log] == [("planned", "active")]
    assert (_stream(db_session).gate_seq, _stream(db_session).applied_seq) == (3, 3)

    # Redelivery is a watermark no-op (idempotent).
    status, resp = _deliver(
        db_session, secret, EVENT_MILESTONE_CREATED,
        _m_payload("acme/repo", "v1-launch", authored_at=T0), 1,
    )
    assert (status, resp["status"]) == (200, "duplicate")


def test_unknown_anchor_is_retryable_and_self_heals(db_session):
    """The anchor scope not yet replicated is an ORDERING gap, retryable (NOT
    ParkNow) — it self-heals when the anchor arrives (matching scope apply's
    unknown-parent posture, §9)."""
    secret = _register(db_session)["secret"]
    payload = _m_payload("acme/repo", "v1", authored_at=T0)

    status, resp = _deliver(
        db_session, secret, EVENT_MILESTONE_CREATED, payload, 1, park_after=5
    )
    assert (status, resp["reason"]) == (503, "apply_failed")
    assert "acme/repo" in resp["error"]

    _anchor(db_session)  # the anchor replicates (by whatever means)
    status, resp = _deliver(
        db_session, secret, EVENT_MILESTONE_CREATED, payload, 1, park_after=5
    )
    assert (status, resp["status"]) == (200, "applied")
    assert milestones.get(db_session, "acme/repo/v1").status == "planned"


# --- §6 LWW convergence + the §4 illegal-history flag -------------------------


def test_concurrent_activate_cancel_converges_lww_and_flags_illegal_history(db_session):
    """§9's headline case: an `activate` (hub) and a `cancel` (spoke) authored
    concurrently during a partition. The row converges by LWW (the later-authored
    `activate` wins), the LOSER's `cancel` transition still lands in the log, and
    the converged history (cancelled→active — the earlier cancel reversed by the
    winning activate) is flagged FIRST-CLASS unreconciled state. Apply CONVERGES —
    it never parks on the LWW loss, and never invents a legal history."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]

    # The milestone exists, planned (authored T0).
    _deliver(db_session, secret, EVENT_MILESTONE_CREATED,
             _m_payload("acme/repo", "v1", authored_at=T0), 1)

    # Concurrent, both authored from planned: cancel EARLIER (T1), activate LATER
    # (T2). Delivered cancel-first — the winner is decided by authored_at, not
    # arrival order.
    t_cancel = T0 + timedelta(minutes=1)
    t_activate = T0 + timedelta(minutes=2)
    s, r = _deliver(db_session, secret, EVENT_MILESTONE_TRANSITIONED,
                    _transitioned("acme/repo", "v1", from_status="planned",
                                  to_status="cancelled", authored_at=t_cancel,
                                  cancelled_at=t_cancel), 2)
    assert (s, r["status"]) == (200, "applied")
    # One transition so far — no adjacent pair, nothing to flag yet.
    assert milestones.list_unreconciled(db_session) == []

    s, r = _deliver(db_session, secret, EVENT_MILESTONE_TRANSITIONED,
                    _transitioned("acme/repo", "v1", from_status="planned",
                                  to_status="active", authored_at=t_activate,
                                  activated_at=t_activate), 3)
    assert (s, r["status"]) == (200, "applied")

    # The row converged to the LWW winner (active); NOTHING parked.
    assert milestones.get(db_session, "acme/repo/v1").status == "active"
    assert (_stream(db_session).gate_seq, _stream(db_session).applied_seq) == (3, 3)

    # The loser's cancel transition is retained in the log alongside the winner.
    log = milestones.transitions(db_session, "acme/repo/v1")
    assert [(t["from_status"], t["to_status"]) for t in log] == [
        ("planned", "cancelled"),
        ("planned", "active"),
    ]

    # First-class unreconciled state raised for the illegal converged history.
    flags = milestones.list_unreconciled(db_session)
    assert len(flags) == 1
    assert flags[0]["milestone"] == "acme/repo/v1"
    assert flags[0]["detail"]["illegal_move"] == ["cancelled", "active"]


def test_lww_loss_keeps_local_and_never_parks(db_session):
    """An older-authored event LOSES LWW: the local row is kept untouched and the
    event is a clean applied ACK — apply NEVER parks on a mere LWW loss (§9)."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]
    _deliver(db_session, secret, EVENT_MILESTONE_CREATED,
             _m_payload("acme/repo", "v1", outcome="new", authored_at=T0 + timedelta(hours=1)), 1)

    # An OLDER update arrives (authored before the create's clock) — it loses.
    s, r = _deliver(db_session, secret, EVENT_MILESTONE_UPDATED,
                    _m_payload("acme/repo", "v1", outcome="stale", authored_at=T0), 2)
    assert (s, r["status"]) == (200, "applied")  # ACKed, not parked
    assert milestones.get(db_session, "acme/repo/v1").outcome == "new"  # local kept


# --- §8.1 DAG race: reject + park an edge whose union cycles -------------------


def test_dependency_race_parks_the_cycling_edge(db_session):
    """§9 DAG race: `add A→B` applied locally; an incoming `add B→A` (which passed
    the peer's own guard) would cycle the UNION — rejected and parked, keeping the
    dependency walk loop-free by construction (§8.1)."""
    _anchor(db_session)
    milestones.create(db_session, "acme/repo", "a")
    milestones.create(db_session, "acme/repo", "b")
    db_session.commit()
    secret = _register(db_session)["secret"]

    s, r = _deliver(db_session, secret, EVENT_MILESTONE_DEPENDENCY_CHANGED,
                    {"op": "add", "dependent": "acme/repo/a",
                     "dependency": "acme/repo/b", "authored_at": _iso(T0)}, 1)
    assert (s, r["status"]) == (200, "applied")

    s, r = _deliver(db_session, secret, EVENT_MILESTONE_DEPENDENCY_CHANGED,
                    {"op": "add", "dependent": "acme/repo/b",
                     "dependency": "acme/repo/a", "authored_at": _iso(T0)}, 2,
                    park_after=3)
    assert (s, r["status"]) == (200, "parked")

    parked = ingest.list_parked(db_session)
    assert len(parked) == 1 and "cycle" in parked[0]["reason"].lower()
    # Only the A→B edge exists; the cycling B→A never landed.
    edges = {
        (e.dependent_id, e.dependency_id)
        for e in db_session.scalars(select(MilestoneDependency))
    }
    a = milestones.get(db_session, "acme/repo/a")
    b = milestones.get(db_session, "acme/repo/b")
    assert edges == {(a.id, b.id)}
    # The gate advanced past the park — the stream flows — but the applied
    # frontier PINS at seq 1 (the parked seq 2 minus one).
    assert (_stream(db_session).gate_seq, _stream(db_session).applied_seq) == (2, 1)


# --- merge apply: tombstone LWW, alias-cycle park, no state-compat re-check ----


def test_merge_apply_tombstone_lww_and_alias_cycle_park(db_session):
    """`milestone.merged` apply (§9): tombstones `from`→`into` by re-derived
    addresses; a re-merge of an already-tombstoned `from` is LWW on `merged_into`
    by authored_at; an application that would cycle the ALIAS graph is parked."""
    _anchor(db_session)
    for name in ("a", "b", "c"):
        milestones.create(db_session, "acme/repo", name)
    db_session.commit()
    secret = _register(db_session)["secret"]

    # a merges into b (authored T1).
    t1 = T0 + timedelta(minutes=1)
    s, r = _deliver(db_session, secret, EVENT_MILESTONE_MERGED,
                    {"from": "acme/repo/a", "into": "acme/repo/b",
                     "authored_at": _iso(t1)}, 1)
    assert (s, r["status"]) == (200, "applied")
    assert milestones.resolve(db_session, "acme/repo/a").name == "b"

    # An OLDER competing merge (a→c, authored T0 < T1) loses LWW — a stays on b.
    s, r = _deliver(db_session, secret, EVENT_MILESTONE_MERGED,
                    {"from": "acme/repo/a", "into": "acme/repo/c",
                     "authored_at": _iso(T0)}, 2)
    assert (s, r["status"]) == (200, "applied")
    assert milestones.resolve(db_session, "acme/repo/a").name == "b"

    # A NEWER competing merge (a→c, authored T2 > T1) wins — a re-points to c.
    t2 = T0 + timedelta(minutes=2)
    s, r = _deliver(db_session, secret, EVENT_MILESTONE_MERGED,
                    {"from": "acme/repo/a", "into": "acme/repo/c",
                     "authored_at": _iso(t2)}, 3)
    assert (s, r["status"]) == (200, "applied")
    assert milestones.resolve(db_session, "acme/repo/a").name == "c"

    # An alias-graph cycle (merge b into a, whose terminal is now... b) parks.
    # b→a: `into` a resolves to c; harmless. Construct a real cycle: c into a,
    # where a's terminal is c → into terminal == from → alias cycle.
    s, r = _deliver(db_session, secret, EVENT_MILESTONE_MERGED,
                    {"from": "acme/repo/c", "into": "acme/repo/a",
                     "authored_at": _iso(t2)}, 4, park_after=3)
    assert (s, r["status"]) == (200, "parked")
    assert "alias" in ingest.list_parked(db_session)[-1]["reason"].lower()


def test_merge_apply_does_not_re_check_state_compat(db_session):
    """State compatibility is decided at the AUTHORING instance (the `merge`
    verb's guard); apply CONVERGES rather than re-litigating it (§9). A merge the
    verb would reject (achieved→planned) applies cleanly on the receiver."""
    _anchor(db_session)
    milestones.create(db_session, "acme/repo", "shipped")
    milestones.create(db_session, "acme/repo", "planning")
    milestones.activate(db_session, "acme/repo/shipped")
    milestones.achieve(db_session, "acme/repo/shipped")
    db_session.commit()
    secret = _register(db_session)["secret"]

    # The verb would reject achieved→planned; apply must NOT re-check it.
    s, r = _deliver(db_session, secret, EVENT_MILESTONE_MERGED,
                    {"from": "acme/repo/shipped", "into": "acme/repo/planning",
                     "authored_at": _iso(T0)}, 1)
    assert (s, r["status"]) == (200, "applied")
    assert milestones.resolve(db_session, "acme/repo/shipped").name == "planning"


# --- seed / enumeration: the platform advertises milestone events -------------


def test_seed_vocabulary_includes_milestones(db_session):
    """§9 seed/enumeration: the platform's self-manifest advertises the milestone
    events, so a pairing/seed handshake subscribes a peer's forward stream to them
    — a newly-paired instance receives the milestone registry, not just scopes. A
    stream subscribed to that declared vocabulary carries a milestone write."""
    assert set(replication.MILESTONE_EVENTS) <= set(replication.PLATFORM_EVENTS)
    assert set(replication.MILESTONE_EVENTS) <= set(replication.manifest_payload()["events"])

    # A forward subscription created from the platform's declared vocabulary (what
    # `prime_forward` does at seed) carries a post-seed milestone write.
    emit.create_outbound_subscription(
        db_session,
        "http://spoke/replication/events/ingest",
        "s",
        list(replication.PLATFORM_EVENTS),
        epoch="e1",
        source_id="primary.platform",
    )
    scopes.create(db_session, slug="acme", name="Acme", kind="org")
    db_session.commit()
    milestones.create(db_session, "acme", "post-seed")
    db_session.commit()

    kinds = [
        r.payload["event_type"]
        for r in db_session.scalars(select(ReplicationOutboxRow))
    ]
    assert EVENT_MILESTONE_CREATED in kinds
