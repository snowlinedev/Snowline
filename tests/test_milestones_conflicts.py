"""Milestone unreconciled-conflict triage surface (milestones.md §9, #248):
route + MCP tool + disposition verb, the startup sweep, the /health count, and
the disposition-columns migration."""

import json

import anyio
import sqlalchemy as sa
from alembic import command
from sqlalchemy import select

from snowline_platform import milestones, scopes
from snowline_platform.app import create_app, sweep_stale_milestone_flags
from snowline_platform.db import session_scope
from snowline_platform.models import MilestoneTransition, MilestoneUnreconciled
from snowline_platform.registry import PluginRegistry
from snowline_platform.trust import TrustResolver

from .test_milestones_line import scratch_db  # noqa: F401 — fixture re-export
from .test_milestones_routes import _trusted_client
from .test_platform_tools import (
    _app,
    _connector_with_platform,
    _run_against_surface,
)

ILLEGAL = ["cancelled", "active"]
LEGAL = ["active", "planned"]  # legal since `deactivate`


def _flag(session, address, move=ILLEGAL, reason="real conflict"):
    m = milestones.get(session, address)
    u = MilestoneUnreconciled(
        milestone_id=m.id, reason=reason, detail={"illegal_move": move}
    )
    session.add(u)
    session.flush()
    return str(u.id)


def _seed(name="v1"):
    """One milestone whose APPLIED row disagrees with the log's last transition:
    log ends `cancelled`, row says `active` (the §9 conflict shape)."""
    with session_scope() as s:
        scopes.create(s, slug="acme/widget", name="Widget", kind="project")
        milestones.create(s, anchor="acme/widget", name=name)
        addr = f"acme/widget/{name}"
        milestones.activate(s, addr)
        milestones.cancel(s, addr)
        m = milestones.get(s, addr)
        m.status = "active"  # the row a peer's LWW-winning event left behind
        return addr, _flag(s, addr)


def test_conflicts_route_lists_unreconciled_and_hides_resolved(clean_db):
    addr, fid = _seed()
    with session_scope() as s:
        _flag(s, addr, move=LEGAL, reason="stale, now legal")  # filtered on read
    client = _trusted_client()
    body = client.get("/milestones/conflicts").json()
    assert body["count"] == 1
    row = body["conflicts"][0]
    assert row["id"] == fid and row["milestone"] == addr
    assert row["detail"]["illegal_move"] == ILLEGAL and row["resolved_at"] is None

    assert client.get("/milestones/conflicts?anchor=acme").json()["count"] == 1
    assert client.get("/milestones/conflicts?anchor=other").json()["count"] == 0

    r = client.post(
        f"/milestones/conflicts/{fid}/resolve",
        json={"disposition": "dismiss", "reason": "triaged"},
    )
    assert r.status_code == 200, r.text
    assert client.get("/milestones/conflicts").json() == {"conflicts": [], "count": 0}
    shown = client.get("/milestones/conflicts?include_resolved=true").json()
    assert shown["count"] == 1 and shown["conflicts"][0]["disposition"] == "dismiss"
    # Closing twice is a 409, not a silent re-close.
    again = client.post(
        f"/milestones/conflicts/{fid}/resolve",
        json={"disposition": "keep_row", "reason": "x"},
    )
    assert again.status_code == 409


def test_resolve_keep_row(clean_db):
    addr, fid = _seed()
    r = _trusted_client().post(
        f"/milestones/conflicts/{fid}/resolve",
        json={"disposition": "keep_row", "reason": "active is right"},
    )
    body = r.json()
    assert r.status_code == 200
    assert body["disposition"] == "keep_row"
    assert body["resolution_reason"] == "active is right"
    assert body["actor"] == "test-owner" and body["resolved_at"]
    with session_scope() as s:
        assert milestones.get(s, addr).status == "active"
        assert len(milestones.transitions(s, addr)) == 2  # no row change


def test_resolve_replay_transition_emits_fresh_lifecycle_event(clean_db):
    addr, fid = _seed()
    with session_scope() as s:
        before = milestones.get(s, addr)
        old_stamp = before.lifecycle_authored_at
        n_before = len(milestones.transitions(s, addr))
    r = _trusted_client().post(
        f"/milestones/conflicts/{fid}/resolve",
        json={"disposition": "replay_transition", "reason": "cancel was right"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["disposition"] == "replay_transition"
    with session_scope() as s:
        m = milestones.get(s, addr)
        assert m.status == "cancelled"
        assert m.lifecycle_authored_at > old_stamp  # fresh lifecycle stamp
        log = list(s.scalars(
            select(MilestoneTransition)
            .where(MilestoneTransition.milestone_id == m.id)
            .order_by(MilestoneTransition.authored_at)
        ))
        assert len(log) == n_before + 1
        assert (log[-1].from_status, log[-1].to_status) == ("active", "cancelled")
        assert log[-1].reason == "cancel was right"
        assert log[-1].authored_at == m.lifecycle_authored_at
        assert milestones.list_unreconciled(s) == []


def test_replay_is_rejected_when_row_already_agrees(clean_db):
    addr, fid = _seed()
    with session_scope() as s:
        milestones.get(s, addr).status = "cancelled"
    r = _trusted_client().post(
        f"/milestones/conflicts/{fid}/resolve",
        json={"disposition": "replay_transition", "reason": "x"},
    )
    assert r.status_code == 409
    with session_scope() as s:  # flag stays open
        assert len(milestones.list_unreconciled(s)) == 1


def test_resolve_dismiss_requires_reason(clean_db):
    addr, fid = _seed()
    client = _trusted_client()
    for body in (
        {"disposition": "dismiss"},
        {"disposition": "dismiss", "reason": "   "},
    ):
        r = client.post(f"/milestones/conflicts/{fid}/resolve", json=body)
        assert r.status_code in (422,), r.text
    bad = client.post(
        f"/milestones/conflicts/{fid}/resolve",
        json={"disposition": "shrug", "reason": "x"},
    )
    assert bad.status_code == 422
    with session_scope() as s:
        assert len(milestones.list_unreconciled(s)) == 1
    ok = client.post(
        f"/milestones/conflicts/{fid}/resolve",
        json={"disposition": "dismiss", "reason": "noise"},
    )
    assert ok.status_code == 200
    with session_scope() as s:
        assert milestones.get(s, addr).status == "active"  # no row change


def test_unknown_conflict_404(clean_db):
    client = _trusted_client()
    for cid in ("00000000-0000-0000-0000-000000000000", "not-a-uuid"):
        r = client.post(
            f"/milestones/conflicts/{cid}/resolve",
            json={"disposition": "dismiss", "reason": "x"},
        )
        assert r.status_code == 404


def test_startup_sweep_deletes_now_legal_flags_idempotently(clean_db):
    addr, real = _seed()
    with session_scope() as s:
        _flag(s, addr, move=LEGAL)
        _flag(s, addr, move=["active", "planned"], reason="another")
    assert sweep_stale_milestone_flags() == 2
    assert sweep_stale_milestone_flags() == 0
    with session_scope() as s:
        remaining = [str(u.id) for u in s.scalars(select(MilestoneUnreconciled))]
    assert remaining == [real]


def test_mcp_tools_round_trip(clean_db):
    addr, fid = _seed()
    app = _app(_connector_with_platform())

    async def _calls(session):
        listed = await session.call_tool("platform__list_milestone_conflicts", {})
        missing_reason = await session.call_tool(
            "platform__resolve_milestone_conflict",
            {"id": fid, "disposition": "dismiss", "reason": ""},
        )
        resolved = await session.call_tool(
            "platform__resolve_milestone_conflict",
            {"id": fid, "disposition": "keep_row", "reason": "ok", "actor": "agent-1"},
        )
        after = await session.call_tool("platform__list_milestone_conflicts", {})
        history = await session.call_tool(
            "platform__list_milestone_conflicts", {"include_resolved": True}
        )
        return listed, missing_reason, resolved, after, history

    listed, missing_reason, resolved, after, history = anyio.run(
        _run_against_surface, app, "/mcp", _calls
    )
    body = json.loads(listed.content[0].text)
    assert body["count"] == 1 and body["conflicts"][0]["id"] == fid
    assert missing_reason.isError is True
    assert resolved.isError is not True, resolved.content[0].text
    assert json.loads(resolved.content[0].text)["actor"] == "agent-1"
    assert json.loads(after.content[0].text) == {"conflicts": [], "count": 0}
    assert json.loads(history.content[0].text)["count"] == 1


def test_health_carries_conflict_count_without_degrading(clean_db):
    from starlette.testclient import TestClient

    client = TestClient(
        create_app(
            resolver=TrustResolver([]), registry=PluginRegistry(),
            migrate_on_startup=False,
        )
    )
    assert client.get("/health").json()["milestones"] == {"conflicts": 0}
    _seed()
    body = client.get("/health").json()
    assert body["milestones"] == {"conflicts": 1}
    assert body["status"] == "ok"
    assert "degraded_reason" not in body or not body["degraded_reason"]


# --- migration ---------------------------------------------------------------

_PREV = "c9d1e3f5a7b9"
_THIS = "d0e2f4a6b8c1"
_NEW = {"resolved_at", "disposition", "resolution_reason", "actor"}


def _cols(engine):
    return {c["name"] for c in sa.inspect(engine).get_columns("milestone_unreconciled")}


def test_migration_adds_disposition_columns_and_round_trips(scratch_db):  # noqa: F811
    cfg, engine = scratch_db
    command.upgrade(cfg, _PREV)
    assert not (_NEW & _cols(engine))
    command.upgrade(cfg, _THIS)
    engine.dispose()
    assert _NEW <= _cols(engine)
    command.downgrade(cfg, _PREV)
    engine.dispose()
    assert not (_NEW & _cols(engine))
    engine.dispose()
