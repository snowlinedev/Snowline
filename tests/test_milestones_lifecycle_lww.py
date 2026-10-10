"""Separate LWW registers for a milestone's lifecycle and descriptive fields
(milestones.md §9, snowlinedev/Snowline#247).

The bug: instance A transitions a milestone at t1; instance B, not yet synced,
edits `outcome` at t2 > t1 and emits a full-row `milestone.updated` carrying its
stale status. Applied on A by whole-row LWW, B's row silently reverted the
transition — no log entry, no unreconciled flag.

The fix: the LIFECYCLE register (status + `*_at`) moves only on `created` /
`transitioned`, ordered by its own clock; the DESCRIPTIVE register (outcome /
target_date / line_rank) moves only on `created` / `updated`, ordered by its own.
An `updated` from any peer — including a pre-#247 one still carrying status —
never touches lifecycle.

Envelopes are hand-built against one Postgres store (the
`test_milestones_replication.py` pattern). "Both sides" of a race are modelled
as two milestone copies receiving the same events in the two arrival orders.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import command
from sqlalchemy import select

from snowline_platform import milestones, replication
from snowline_plugin_sdk.contract import (
    EVENT_MILESTONE_CREATED,
    EVENT_MILESTONE_TRANSITIONED,
    EVENT_MILESTONE_UPDATED,
)
from snowline_plugin_sdk.replication import emit
from snowline_plugin_sdk.replication.models import ReplicationOutboxRow

from .test_milestones_line import scratch_db  # noqa: F401 — fixture re-export
from .test_milestones_replication import (
    T0,
    _anchor,
    _deliver,
    _iso,
    _m_payload,
    _register,
    _transitioned,
)

REPO = "acme/repo"
T1, T2, T3, T5 = (T0 + timedelta(minutes=n) for n in (1, 2, 3, 5))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new(payload, *, descriptive_at, lifecycle_at):
    """A current (post-#247) peer's payload: the per-register clocks ride beside
    `authored_at`."""
    return {
        **payload,
        "descriptive_authored_at": _iso(descriptive_at),
        "lifecycle_authored_at": _iso(lifecycle_at),
    }


def _state(session, address):
    m = milestones.get(session, address)
    return (m.status, m.activated_at, m.outcome)


def _log(session, address):
    return [
        (t["from_status"], t["to_status"])
        for t in milestones.transitions(session, address)
    ]


# --- the reported bug, A's side with real local verbs --------------------------


def test_update_after_transition_does_not_revert_lifecycle(db_session):
    """A activates at t1 (local verb); B's `updated` authored at t2 > t1 —
    carrying B's stale status=planned — arrives on A: the outcome applies, the
    status stays active, the log is untouched."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]
    milestones.create(db_session, REPO, "v1", outcome="old")
    milestones.activate(db_session, f"{REPO}/v1", reason="go")
    db_session.commit()
    activated_at = milestones.get(db_session, f"{REPO}/v1").activated_at
    created_clock = milestones.get(db_session, f"{REPO}/v1").lww_authored_at

    t2 = _utcnow() + timedelta(minutes=5)
    b_update = _new(
        _m_payload(REPO, "v1", outcome="edited on B", authored_at=t2),
        descriptive_at=t2,
        lifecycle_at=created_clock,  # B never saw the activation
    )
    s, r = _deliver(db_session, secret, EVENT_MILESTONE_UPDATED, b_update, 1)
    assert (s, r["status"]) == (200, "applied")

    assert _state(db_session, f"{REPO}/v1") == ("active", activated_at, "edited on B")
    assert _log(db_session, f"{REPO}/v1") == [("planned", "active")]
    assert milestones.list_unreconciled(db_session) == []


def test_no_unreconciled_flag_for_benign_update_race(db_session):
    """The issue's exact case: A DEACTIVATES; B (still seeing active) edits the
    target date later. Status stays planned, `activated_at` stays cleared, the
    date applies, and the stale status in B's payload raises NO flag — a benign
    race is not a contradiction."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]
    milestones.create(db_session, REPO, "v1")
    milestones.activate(db_session, f"{REPO}/v1")
    milestones.deactivate(db_session, f"{REPO}/v1", reason="wrong line")
    db_session.commit()

    t2 = _utcnow() + timedelta(minutes=5)
    b_update = _new(
        {**_m_payload(REPO, "v1", status="active", activated_at=T1,
                      authored_at=t2),
         "target_date": "2026-12-01"},
        descriptive_at=t2,
        lifecycle_at=T1,
    )
    _deliver(db_session, secret, EVENT_MILESTONE_UPDATED, b_update, 1)

    m = milestones.get(db_session, f"{REPO}/v1")
    assert (m.status, m.activated_at) == ("planned", None)
    assert m.target_date.isoformat() == "2026-12-01"
    assert milestones.list_unreconciled(db_session) == []
    assert _log(db_session, f"{REPO}/v1") == [
        ("planned", "active"), ("active", "planned"),
    ]


# --- both arrival orders converge ---------------------------------------------


def test_transition_after_update_wins_on_lifecycle(db_session):
    """B edits the outcome at T1; A activates at T2 (carrying A's stale outcome).
    On both sides — i.e. in both arrival orders — the copy converges to
    (active @T2, B's outcome): the later transition wins lifecycle, and the
    transition's stale descriptive fields never clobber B's edit."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]
    seq = iter(range(1, 100))

    def events(name):
        created = _new(_m_payload(REPO, name, outcome="orig", authored_at=T0),
                       descriptive_at=T0, lifecycle_at=T0)
        updated = _new(_m_payload(REPO, name, outcome="B's edit", authored_at=T1),
                       descriptive_at=T1, lifecycle_at=T0)
        trans = _new(
            _transitioned(REPO, name, from_status="planned", to_status="active",
                          authored_at=T2, activated_at=T2, outcome="orig"),
            descriptive_at=T0, lifecycle_at=T2,
        )
        return created, updated, trans

    # A's side: its own update arrived first (B's update, then A's transition).
    c, u, t = events("on-a")
    _deliver(db_session, secret, EVENT_MILESTONE_CREATED, c, next(seq))
    _deliver(db_session, secret, EVENT_MILESTONE_UPDATED, u, next(seq))
    _deliver(db_session, secret, EVENT_MILESTONE_TRANSITIONED, t, next(seq))
    # B's side: the transition lands first, the (earlier-authored) update after.
    c, u, t = events("on-b")
    _deliver(db_session, secret, EVENT_MILESTONE_CREATED, c, next(seq))
    _deliver(db_session, secret, EVENT_MILESTONE_TRANSITIONED, t, next(seq))
    _deliver(db_session, secret, EVENT_MILESTONE_UPDATED, u, next(seq))

    for name in ("on-a", "on-b"):
        assert _state(db_session, f"{REPO}/{name}") == ("active", T2, "B's edit")
        assert _log(db_session, f"{REPO}/{name}") == [("planned", "active")]
    assert milestones.list_unreconciled(db_session) == []


def test_old_peer_updated_payload_with_status_is_ignored_for_lifecycle(db_session):
    """A pre-#247 peer's `updated` has no per-register keys and one whole-row
    `authored_at` — even far in the future and carrying status=cancelled, it
    moves only the descriptive register."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]
    _deliver(db_session, secret, EVENT_MILESTONE_CREATED,
             _m_payload(REPO, "v1", authored_at=T0), 1)
    _deliver(db_session, secret, EVENT_MILESTONE_TRANSITIONED,
             _transitioned(REPO, "v1", from_status="planned", to_status="active",
                           authored_at=T1, activated_at=T1), 2)

    old_peer = _m_payload(REPO, "v1", outcome="from an old peer",
                          status="cancelled", cancelled_at=T0,
                          authored_at=T0 + timedelta(days=365))
    assert "lifecycle_authored_at" not in old_peer
    s, r = _deliver(db_session, secret, EVENT_MILESTONE_UPDATED, old_peer, 3)
    assert (s, r["status"]) == (200, "applied")

    m = milestones.get(db_session, f"{REPO}/v1")
    assert (m.status, m.activated_at, m.cancelled_at) == ("active", T1, None)
    assert m.outcome == "from an old peer"
    assert m.lifecycle_authored_at == T1  # lifecycle clock untouched
    assert milestones.list_unreconciled(db_session) == []


def test_created_seeds_both_registers(db_session):
    """A `created` seeds both registers from its own stamps (falling back to
    `authored_at` for an old peer). Thereafter they advance independently: an
    `updated` at T3 does not shadow a transition authored at T2 < T3 — under
    whole-row LWW that transition would have lost."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]
    _deliver(db_session, secret, EVENT_MILESTONE_CREATED,
             _m_payload(REPO, "old", outcome="x", authored_at=T0), 1)
    m = milestones.get(db_session, f"{REPO}/old")
    assert (m.lww_authored_at, m.lifecycle_authored_at) == (T0, T0)
    assert (m.lww_source_id, m.lifecycle_source_id) == ("peer.platform",) * 2

    _deliver(db_session, secret, EVENT_MILESTONE_CREATED,
             _new(_m_payload(REPO, "new", authored_at=T1),
                  descriptive_at=T1, lifecycle_at=T0), 2)
    m = milestones.get(db_session, f"{REPO}/new")
    assert (m.lww_authored_at, m.lifecycle_authored_at) == (T1, T0)

    _deliver(db_session, secret, EVENT_MILESTONE_UPDATED,
             _m_payload(REPO, "old", outcome="y", authored_at=T3), 3)
    _deliver(db_session, secret, EVENT_MILESTONE_TRANSITIONED,
             _transitioned(REPO, "old", from_status="planned", to_status="active",
                           authored_at=T2, activated_at=T2, outcome="x"), 4)
    m = milestones.get(db_session, f"{REPO}/old")
    assert (m.status, m.outcome) == ("active", "y")
    assert (m.lww_authored_at, m.lifecycle_authored_at) == (T3, T2)


def test_created_on_existing_row_converges_each_register_by_its_own_clock(db_session):
    """A same-name `created` for a row that already exists here (a concurrent
    create) LWW-compares each register separately: a later-born row's planned
    status cannot erase a transition authored after it."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]
    _deliver(db_session, secret, EVENT_MILESTONE_CREATED,
             _m_payload(REPO, "v1", outcome="first", authored_at=T0), 1)
    _deliver(db_session, secret, EVENT_MILESTONE_TRANSITIONED,
             _transitioned(REPO, "v1", from_status="planned", to_status="active",
                           authored_at=T3, activated_at=T3, outcome="first"), 2)
    _deliver(db_session, secret, EVENT_MILESTONE_CREATED,
             _m_payload(REPO, "v1", outcome="second", authored_at=T1), 3)
    assert _state(db_session, f"{REPO}/v1") == ("active", T3, "second")


def test_descriptive_lww_still_ordered_by_its_own_stamp(db_session):
    """The descriptive register is ordered by the descriptive stamp alone: an
    `updated` at T1 applies even after a transition at T5 (a later lifecycle
    clock no longer shadows it), an older `updated` still loses, and a
    `transitioned` never moves descriptive fields on an existing row."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]
    _deliver(db_session, secret, EVENT_MILESTONE_CREATED,
             _m_payload(REPO, "v1", outcome="orig", authored_at=T0), 1)
    _deliver(db_session, secret, EVENT_MILESTONE_TRANSITIONED,
             _new(_transitioned(REPO, "v1", from_status="planned",
                                to_status="active", authored_at=T5,
                                activated_at=T5, outcome="from the transition"),
                  descriptive_at=T5, lifecycle_at=T5), 2)
    assert milestones.get(db_session, f"{REPO}/v1").outcome == "orig"

    _deliver(db_session, secret, EVENT_MILESTONE_UPDATED,
             _new(_m_payload(REPO, "v1", outcome="T1 edit", authored_at=T1),
                  descriptive_at=T1, lifecycle_at=T0), 3)
    assert _state(db_session, f"{REPO}/v1") == ("active", T5, "T1 edit")

    _deliver(db_session, secret, EVENT_MILESTONE_UPDATED,
             _new(_m_payload(REPO, "v1", outcome="stale",
                             authored_at=T0 + timedelta(seconds=30)),
                  descriptive_at=T0 + timedelta(seconds=30), lifecycle_at=T0), 4)
    assert milestones.get(db_session, f"{REPO}/v1").outcome == "T1 edit"

    _deliver(db_session, secret, EVENT_MILESTONE_UPDATED,
             _new(_m_payload(REPO, "v1", outcome="T2 edit", authored_at=T2),
                  descriptive_at=T2, lifecycle_at=T0), 5)
    assert _state(db_session, f"{REPO}/v1") == ("active", T5, "T2 edit")


def test_line_rank_absent_key_rule_unchanged(db_session):
    """release-line.md §2.3 survives the split: a current-peer `updated` WITHOUT
    `line_rank` preserves the local rank (and, being `updated`, the local
    lifecycle); an explicit null clears it."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]
    milestones.create(db_session, REPO, "v1")
    milestones.place_in_line(db_session, f"{REPO}/v1")
    milestones.activate(db_session, f"{REPO}/v1")
    db_session.commit()
    rank = milestones.get(db_session, f"{REPO}/v1").line_rank
    assert rank is not None

    t = _utcnow() + timedelta(minutes=5)
    no_key = _new(_m_payload(REPO, "v1", outcome="edit", authored_at=t),
                  descriptive_at=t, lifecycle_at=T0)
    assert "line_rank" not in no_key
    _deliver(db_session, secret, EVENT_MILESTONE_UPDATED, no_key, 1)
    m = milestones.get(db_session, f"{REPO}/v1")
    assert (m.line_rank, m.outcome, m.status) == (rank, "edit", "active")

    t = t + timedelta(minutes=1)
    cleared = {**_new(_m_payload(REPO, "v1", outcome="edit", authored_at=t),
                      descriptive_at=t, lifecycle_at=T0),
               "line_rank": None}
    _deliver(db_session, secret, EVENT_MILESTONE_UPDATED, cleared, 2)
    m = milestones.get(db_session, f"{REPO}/v1")
    assert (m.line_rank, m.status) == (None, "active")


# --- emit side ------------------------------------------------------------------


def test_verbs_stamp_only_their_own_register_and_payloads_carry_both(db_session):
    """`update` advances only the descriptive clock, a transition only the
    lifecycle clock, and every full-row payload carries both per-register clocks
    beside the unchanged `authored_at`."""
    emit.create_outbound_subscription(
        db_session, "http://peer/replication/events/ingest", "topsecret",
        list(replication.MILESTONE_EVENTS), epoch="e1", source_id="hub.platform",
    )
    _anchor(db_session)
    m = milestones.create(db_session, REPO, "v1")
    born = m.lww_authored_at
    assert m.lifecycle_authored_at == born

    milestones.update(db_session, f"{REPO}/v1", outcome="edited")
    m = milestones.get(db_session, f"{REPO}/v1")
    edited = m.lww_authored_at
    assert edited > born and m.lifecycle_authored_at == born

    milestones.activate(db_session, f"{REPO}/v1")
    m = milestones.get(db_session, f"{REPO}/v1")
    assert m.lww_authored_at == edited and m.lifecycle_authored_at > born
    db_session.commit()

    rows = db_session.scalars(
        select(ReplicationOutboxRow).order_by(ReplicationOutboxRow.seq)
    ).all()
    created, updated, trans = (r.payload["payload"] for r in rows)
    assert created["descriptive_authored_at"] == created["lifecycle_authored_at"]
    assert created["authored_at"] == created["descriptive_authored_at"]
    assert updated["authored_at"] == updated["descriptive_authored_at"] == _iso(edited)
    assert updated["lifecycle_authored_at"] == _iso(born)
    assert updated["status"] == "planned"  # still full-row for pre-#247 receivers
    assert trans["authored_at"] == trans["lifecycle_authored_at"]
    assert trans["descriptive_authored_at"] == _iso(edited)


# --- migration ------------------------------------------------------------------

_PREV = "b8c0d2e4f6a8"
_THIS = "c9d1e3f5a7b9"


def _cols(engine):
    return {c["name"] for c in sa.inspect(engine).get_columns("milestones")}


def test_migration_backfills_lifecycle_clock_and_round_trips(scratch_db):  # noqa: F811
    """The upgrade backfills the lifecycle clock from the latest transition-log
    entry, else the row's existing clock; downgrade drops the columns."""
    cfg, engine = scratch_db
    command.upgrade(cfg, _PREV)
    with engine.begin() as conn:
        sid = conn.execute(sa.text(
            "INSERT INTO scopes (id, slug, name, kind, status, isolated) "
            "VALUES (gen_random_uuid(), 'acme', 'Acme', 'org', 'active', false) "
            "RETURNING id")).scalar()
        ids = {}
        for name in ("moved", "never", "bare"):
            ids[name] = conn.execute(sa.text(
                "INSERT INTO milestones (id, anchor_scope_id, name, status, "
                "lww_authored_at, lww_source_id) VALUES (gen_random_uuid(), :s, "
                ":n, 'planned', :c, :src) RETURNING id"),
                {"s": sid, "n": name,
                 "c": None if name == "bare" else T5,
                 "src": None if name == "bare" else "row.src"}).scalar()
        for at, src in ((T1, "early"), (T2, "late")):
            conn.execute(sa.text(
                "INSERT INTO milestone_transitions (id, milestone_id, from_status, "
                "to_status, authored_at, source_id) VALUES (gen_random_uuid(), :m, "
                "'planned', 'active', :a, :s)"),
                {"m": ids["moved"], "a": at, "s": src})
    command.upgrade(cfg, _THIS)
    engine.dispose()
    assert {"lifecycle_authored_at", "lifecycle_source_id"} <= _cols(engine)
    with engine.connect() as conn:
        got = {
            r.name: (r.lifecycle_authored_at, r.lifecycle_source_id)
            for r in conn.execute(sa.text(
                "SELECT name, lifecycle_authored_at, lifecycle_source_id "
                "FROM milestones"))
        }
    assert got == {
        "moved": (T2, "late"),
        "never": (T5, "row.src"),
        "bare": (None, None),
    }
    command.downgrade(cfg, _PREV)
    engine.dispose()
    assert "lifecycle_authored_at" not in _cols(engine)
    engine.dispose()
