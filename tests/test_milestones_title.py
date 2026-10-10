"""Milestone display title (snowlinedev/Snowline#156, milestones.md §2/§9).

An editable, human-friendly `title` beside the slug identity: ≤ 120 chars,
trimmed, empty → NULL. It rides the DESCRIPTIVE replication register with the
same absent-key rule as `line_rank` (a pre-#156 peer's payload without the key
preserves the local title; only an explicit null clears). Merge: `into` keeps
its own title, inheriting `from`'s only when untitled; the tombstone's clears.
"""

from __future__ import annotations

import json
from datetime import timedelta

import anyio
import pytest
from alembic import command
from sqlalchemy import select
from starlette.testclient import TestClient

from snowline_platform import milestones, replication, scopes
from snowline_platform.app import create_app
from snowline_platform.db import session_scope
from snowline_platform.trust import Principal, TrustResolver
from snowline_plugin_sdk.contract import (
    EVENT_MILESTONE_CREATED,
    EVENT_MILESTONE_MERGED,
    EVENT_MILESTONE_UPDATED,
)
from snowline_plugin_sdk.replication import emit
from snowline_plugin_sdk.replication.models import ReplicationOutboxRow

from .test_milestones_line import _cols, scratch_db  # noqa: F401 — fixture
from .test_milestones_replication import (
    T0,
    _anchor,
    _deliver,
    _iso,
    _m_payload,
    _register,
)
from .test_platform_tools import (
    _app,
    _connector_with_platform,
    _run_against_surface,
)

REPO = "acme/repo"
T1, T2, T3, T5 = (T0 + timedelta(minutes=n) for n in (1, 2, 3, 5))


class _AlwaysTrust:
    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _scopes(session):
    scopes.create(session, slug="acme", name="Acme", kind="org")
    scopes.create(session, slug=REPO, name="Repo", kind="project", parent="acme")


def _subscribe(session):
    emit.create_outbound_subscription(
        session,
        "http://peer/replication/events/ingest",
        "topsecret",
        list(replication.MILESTONE_EVENTS),
        epoch="e1",
        source_id="hub.platform",
    )


def _desc(payload, at, **extra):
    """A current peer's descriptive-register payload at clock `at`."""
    return {
        **payload,
        "descriptive_authored_at": _iso(at),
        "lifecycle_authored_at": _iso(T0),
        **extra,
    }


# --- service ------------------------------------------------------------------


def test_create_with_title_and_update_clear(db_session):
    _scopes(db_session)
    m = milestones.create(db_session, REPO, "v1", title="  Version One  ")
    assert m.title == "Version One"
    addr = f"{REPO}/v1"

    clock = m.lww_authored_at
    # Equal value after normalization is a no-op: no stamp.
    milestones.update(db_session, addr, title="Version One ")
    assert milestones.get(db_session, addr).lww_authored_at == clock

    milestones.update(db_session, addr, title="Renamed")
    assert milestones.get(db_session, addr).title == "Renamed"
    # Omitted leaves it; explicit None clears.
    milestones.update(db_session, addr, outcome="ship")
    assert milestones.get(db_session, addr).title == "Renamed"
    milestones.update(db_session, addr, title=None)
    assert milestones.get(db_session, addr).title is None


def test_title_length_and_trim_rules(db_session):
    _scopes(db_session)
    assert milestones.normalize_title(None) is None
    assert milestones.normalize_title("   ") is None
    assert milestones.normalize_title("") is None
    assert milestones.normalize_title(" a ") == "a"
    assert milestones.normalize_title("x" * 120) == "x" * 120
    # The limit is measured after trimming.
    assert milestones.normalize_title("  " + "x" * 120 + "  ") == "x" * 120
    with pytest.raises(milestones.InvalidMilestoneFieldError, match="120"):
        milestones.normalize_title("x" * 121)
    with pytest.raises(milestones.InvalidMilestoneFieldError):
        milestones.create(db_session, REPO, "v1", title="x" * 121)
    milestones.create(db_session, REPO, "v2", title="   ")
    assert milestones.get(db_session, f"{REPO}/v2").title is None
    with pytest.raises(milestones.InvalidMilestoneFieldError):
        milestones.update(db_session, f"{REPO}/v2", title="y" * 121)


def test_rows_carry_title_and_display_name(db_session):
    _scopes(db_session)
    milestones.create(db_session, REPO, "v1", title="Version One")
    milestones.create(db_session, REPO, "v2")
    rows = {r["name"]: r for r in milestones.list_milestones(db_session)}
    assert rows["v1"]["title"] == "Version One"
    assert rows["v1"]["display_name"] == "Version One"
    assert rows["v2"]["title"] is None
    assert rows["v2"]["display_name"] == "v2"
    m, _ = milestones.resolve_row(db_session, "v1", context=REPO)
    assert milestones.to_row(m)["display_name"] == "Version One"


def test_http_create_patch_and_reject(clean_db):
    with session_scope() as s:
        _scopes(s)
    client = TestClient(
        create_app(resolver=TrustResolver([_AlwaysTrust()]), migrate_on_startup=False)
    )
    r = client.post(
        "/milestones", json={"anchor": REPO, "name": "v1", "title": " Beta "}
    )
    assert r.status_code == 201, r.text
    assert (r.json()["title"], r.json()["display_name"]) == ("Beta", "Beta")
    addr = f"/milestones/{REPO}/v1"
    r = client.patch(addr, json={"title": "GA"})
    assert r.status_code == 200 and r.json()["title"] == "GA"
    r = client.patch(addr, json={"title": "z" * 121})
    assert r.status_code == 422, r.text
    r = client.patch(addr, json={"title": None})
    assert r.json()["title"] is None and r.json()["display_name"] == "v1"
    assert client.get(addr).json()["title"] is None


# --- replication --------------------------------------------------------------


def test_title_emitted_in_descriptive_payload(db_session):
    _scopes(db_session)
    _subscribe(db_session)
    milestones.create(db_session, REPO, "v1", title="One")
    milestones.update(db_session, f"{REPO}/v1", title="Uno")
    rows = db_session.scalars(
        select(ReplicationOutboxRow).order_by(ReplicationOutboxRow.seq)
    ).all()
    payloads = [
        (r.payload["event_type"], r.payload["payload"]["title"]) for r in rows
    ]
    assert payloads == [
        (EVENT_MILESTONE_CREATED, "One"),
        (EVENT_MILESTONE_UPDATED, "Uno"),
    ]


def test_title_rides_descriptive_register_and_absent_key_preserves(db_session):
    """Two concurrent title edits (T1 "A", T2 "B") converge to "B" in BOTH
    arrival orders; a later pre-#156 peer's outcome edit (no `title` key)
    preserves it; a later explicit null clears it."""
    _anchor(db_session)
    secret = _register(db_session)["secret"]
    seq = iter(range(1, 100))

    def deliver(event, payload):
        s, r = _deliver(db_session, secret, event, payload, next(seq))
        assert (s, r["status"]) == (200, "applied"), r

    for name, order in (("ab", ("A", "B")), ("ba", ("B", "A"))):
        deliver(
            EVENT_MILESTONE_CREATED,
            _desc(_m_payload(REPO, name, authored_at=T0), T0, title="orig"),
        )
        edits = {
            "A": _desc(_m_payload(REPO, name, authored_at=T1), T1, title="A"),
            "B": _desc(_m_payload(REPO, name, authored_at=T2), T2, title="B"),
        }
        for k in order:
            deliver(EVENT_MILESTONE_UPDATED, edits[k])
        assert milestones.get(db_session, f"{REPO}/{name}").title == "B"

    # Pre-#156 peer: no `title` key at all, later clock → outcome applies,
    # title preserved.
    old_peer = _m_payload(REPO, "ab", outcome="old peer edit", authored_at=T3)
    assert "title" not in old_peer
    deliver(EVENT_MILESTONE_UPDATED, old_peer)
    m = milestones.get(db_session, f"{REPO}/ab")
    assert (m.outcome, m.title) == ("old peer edit", "B")

    # Explicit null clears.
    deliver(
        EVENT_MILESTONE_UPDATED,
        _desc(_m_payload(REPO, "ab", authored_at=T5), T5, title=None),
    )
    assert milestones.get(db_session, f"{REPO}/ab").title is None

    # A row first seen via a pre-#156 `created` inserts untitled.
    deliver(EVENT_MILESTONE_CREATED, _m_payload(REPO, "fresh", authored_at=T0))
    assert milestones.get(db_session, f"{REPO}/fresh").title is None


# --- merge --------------------------------------------------------------------


def test_merge_title_inherits_when_into_untitled(db_session):
    _scopes(db_session)
    _subscribe(db_session)
    milestones.create(db_session, REPO, "old", title="Old Title")
    milestones.create(db_session, REPO, "new")
    milestones.create(db_session, REPO, "old2", title="Other")
    milestones.create(db_session, REPO, "kept", title="Kept")

    out = milestones.merge(db_session, f"{REPO}/old", f"{REPO}/new")
    assert out["tombstone"]["title"] is None
    assert milestones.get(db_session, f"{REPO}/new").title == "Old Title"
    # The inheriting merge also emits `updated` for `into` (carrying the title).
    rows = db_session.scalars(
        select(ReplicationOutboxRow).order_by(ReplicationOutboxRow.seq)
    ).all()
    tail = [
        (r.payload["event_type"], r.payload["payload"].get("title"))
        for r in rows[-2:]
    ]
    assert tail[0][0] == EVENT_MILESTONE_MERGED
    assert tail[1] == (EVENT_MILESTONE_UPDATED, "Old Title")

    # `into` with its own title keeps it.
    milestones.merge(db_session, f"{REPO}/old2", f"{REPO}/kept")
    assert milestones.get(db_session, f"{REPO}/kept").title == "Kept"
    assert milestones.get(db_session, f"{REPO}/old2").title is None


# --- MCP ----------------------------------------------------------------------


def test_mcp_create_update_round_trip(clean_db):
    with session_scope() as s:
        _scopes(s)
    app = _app(_connector_with_platform())

    async def _calls(session):
        out = []
        for tool, args in (
            ("platform__create_milestone",
             {"anchor": REPO, "name": "v1", "title": "Version One"}),
            ("platform__update_milestone",
             {"address": f"{REPO}/v1", "outcome": "ship"}),
            ("platform__update_milestone",
             {"address": f"{REPO}/v1", "title": "V1 GA"}),
            ("platform__list_milestones", {"anchor": REPO}),
            ("platform__update_milestone",
             {"address": f"{REPO}/v1", "title": ""}),
        ):
            res = await session.call_tool(tool, args)
            assert res.isError is not True, res.content[0].text
            out.append(json.loads(res.content[0].text))
        return out

    created, kept, renamed, listed, cleared = anyio.run(
        _run_against_surface, app, "/mcp", _calls
    )
    assert created["title"] == "Version One"
    assert kept["title"] == "Version One"  # omitted → unchanged
    assert renamed["display_name"] == "V1 GA"
    assert listed["milestones"][0]["title"] == "V1 GA"
    assert (cleared["title"], cleared["display_name"]) == (None, "v1")


# --- migration ----------------------------------------------------------------


_PREV = "d0e2f4a6b8c1"
_THIS = "e1f3a5b7c9d2"


def test_migration_up_down(scratch_db):
    cfg, engine = scratch_db
    command.upgrade(cfg, _PREV)
    assert "title" not in _cols(engine)
    command.upgrade(cfg, _THIS)
    engine.dispose()
    assert "title" in _cols(engine)
    command.downgrade(cfg, _PREV)
    engine.dispose()
    assert "title" not in _cols(engine)
    engine.dispose()
