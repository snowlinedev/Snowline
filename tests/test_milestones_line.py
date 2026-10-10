"""Release-line succession ordering on the milestone registry (release-line.md
§2/§3, snowlinedev/Snowline#265): the `line_rank` fractional index, the
`place_in_line` / `remove_from_line` verbs, the exact-anchor `in_line` listing,
merge handling, the replication payload + old-peer-safe apply rule, the HTTP
surface, and the migration that reserves the name `line`."""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from starlette.testclient import TestClient

from snowline_platform import fracrank, milestones, replication, scopes
from snowline_platform.app import create_app
from snowline_platform.db import session_scope
from snowline_platform.models import Milestone
from snowline_platform.trust import Principal, TrustResolver
from snowline_plugin_sdk.contract import (
    EVENT_MILESTONE_MERGED,
    EVENT_MILESTONE_UPDATED,
)
from snowline_plugin_sdk.replication import emit
from snowline_plugin_sdk.replication.models import ReplicationOutboxRow

from .test_milestones_replication import T0, _deliver, _m_payload, _register

REPO = "acme/repo"


def _seed(session, *names, anchor=REPO):
    if scopes.resolve(session, "acme") is None:
        scopes.create(session, slug="acme", name="Acme", kind="org")
    if anchor != "acme" and scopes.resolve(session, anchor) is None:
        scopes.create(
            session, slug=anchor, name=anchor.split("/")[-1].title(),
            kind="project", parent="acme",
        )
    for n in names:
        milestones.create(session, anchor, n)
    session.commit()


def _addr(name, anchor=REPO):
    return f"{anchor}/{name}"


def _line(session, anchor=REPO):
    return [
        r["address"]
        for r in milestones.list_milestones(session, anchor=anchor, in_line=True)
    ]


# --- service ------------------------------------------------------------------


def test_place_in_line_appends_and_orders_by_rank(db_session):
    _seed(db_session, "v3", "v1", "v2")
    for n in ("v1", "v2", "v3"):
        out = milestones.place_in_line(db_session, _addr(n))
    assert out["line"] == [_addr("v1"), _addr("v2"), _addr("v3")]
    assert out["line_rank"] is not None and out["address"] == _addr("v3")
    assert _line(db_session) == out["line"]
    # Ranks strictly increase along the line and travel as strings.
    ranks = [
        Decimal(r["line_rank"])
        for r in milestones.list_milestones(db_session, anchor=REPO, in_line=True)
    ]
    assert ranks == sorted(ranks) and len(set(ranks)) == 3


def test_place_after_and_before_insert_between_neighbours(db_session):
    _seed(db_session, "a", "b", "c", "d")
    milestones.place_in_line(db_session, _addr("a"))
    milestones.place_in_line(db_session, _addr("d"))
    out = milestones.place_in_line(db_session, _addr("b"), after=_addr("a"))
    assert out["line"] == [_addr("a"), _addr("b"), _addr("d")]
    # A bare neighbour name resolves against the milestone's own anchor.
    out = milestones.place_in_line(db_session, _addr("c"), before="d")
    assert out["line"] == [_addr("a"), _addr("b"), _addr("c"), _addr("d")]
    # Before the first member.
    _seed(db_session, "zero")
    out = milestones.place_in_line(db_session, _addr("zero"), before=_addr("a"))
    assert out["line"][0] == _addr("zero")
    with pytest.raises(milestones.InvalidMilestoneFieldError):
        milestones.place_in_line(db_session, _addr("a"), after="b", before="c")


def test_replace_moves_existing_member(db_session):
    _seed(db_session, "a", "b", "c")
    for n in ("a", "b", "c"):
        milestones.place_in_line(db_session, _addr(n))
    out = milestones.place_in_line(db_session, _addr("a"), after=_addr("c"))
    assert out["line"] == [_addr("b"), _addr("c"), _addr("a")]
    out = milestones.place_in_line(db_session, _addr("a"), before=_addr("b"))
    assert out["line"] == [_addr("a"), _addr("b"), _addr("c")]
    # Neither neighbour: append moves it to the end.
    out = milestones.place_in_line(db_session, _addr("b"))
    assert out["line"] == [_addr("a"), _addr("c"), _addr("b")]
    with pytest.raises(milestones.MilestoneLineError, match="itself"):
        milestones.place_in_line(db_session, _addr("b"), after=_addr("b"))


def test_cross_anchor_neighbour_rejected(db_session):
    _seed(db_session, "v1")
    _seed(db_session, "org-rel", anchor="acme")
    _seed(db_session, "w1", anchor="acme/other")
    milestones.place_in_line(db_session, "acme/org-rel")
    milestones.place_in_line(db_session, "acme/other/w1")
    with pytest.raises(milestones.MilestoneLineError, match="anchored at"):
        milestones.place_in_line(db_session, _addr("v1"), after="acme/org-rel")
    with pytest.raises(milestones.MilestoneLineError, match="anchored at"):
        milestones.place_in_line(db_session, _addr("v1"), before="acme/other/w1")
    # Alias-following: a tombstone at THIS anchor merged into another anchor's
    # milestone resolves cross-anchor and is rejected too.
    _seed(db_session, "old")
    milestones.merge(db_session, _addr("old"), "acme/other/w1")
    with pytest.raises(milestones.MilestoneLineError, match="anchored at"):
        milestones.place_in_line(db_session, _addr("v1"), after=_addr("old"))
    # A neighbour not in the line: the error lists the line.
    _seed(db_session, "v2", "v3")
    milestones.place_in_line(db_session, _addr("v2"))
    with pytest.raises(milestones.MilestoneLineError) as exc:
        milestones.place_in_line(db_session, _addr("v1"), after=_addr("v3"))
    assert "not in" in str(exc.value) and _addr("v2") in str(exc.value)


def test_tombstone_cannot_be_placed(db_session):
    _seed(db_session, "dup", "real")
    milestones.merge(db_session, _addr("dup"), _addr("real"))
    with pytest.raises(milestones.MilestoneLineError, match="tombstone"):
        milestones.place_in_line(db_session, _addr("dup"))
    with pytest.raises(milestones.MilestoneLineError, match="tombstone"):
        milestones.remove_from_line(db_session, _addr("dup"))


def test_remove_from_line_clears_rank(db_session):
    _seed(db_session, "a", "b")
    milestones.place_in_line(db_session, _addr("a"))
    milestones.place_in_line(db_session, _addr("b"))
    out = milestones.remove_from_line(db_session, _addr("a"))
    assert out["line_rank"] is None and out["line"] == [_addr("b")]
    # Idempotent.
    assert milestones.remove_from_line(db_session, _addr("a"))["line"] == [_addr("b")]
    # A removed member cannot be a neighbour.
    with pytest.raises(milestones.MilestoneLineError):
        milestones.place_in_line(db_session, _addr("b"), after=_addr("a"))


def test_line_rank_survives_achieve_and_cancel(db_session):
    _seed(db_session, "v1", "v2", "v3")
    for n in ("v1", "v2", "v3"):
        milestones.place_in_line(db_session, _addr(n))
    before = {r["address"]: r["line_rank"] for r in
              milestones.list_milestones(db_session, anchor=REPO, in_line=True)}
    milestones.activate(db_session, _addr("v1"))
    milestones.achieve(db_session, _addr("v1"))
    milestones.cancel(db_session, _addr("v2"))
    rows = milestones.list_milestones(db_session, anchor=REPO, in_line=True)
    assert [(r["address"], r["status"]) for r in rows] == [
        (_addr("v1"), "achieved"), (_addr("v2"), "cancelled"), (_addr("v3"), "planned"),
    ]
    assert {r["address"]: r["line_rank"] for r in rows} == before
    # Placement never transitions.
    milestones.place_in_line(db_session, _addr("v3"), before=_addr("v1"))
    assert milestones.get(db_session, _addr("v3")).status == "planned"
    # `status` composes with the line read.
    assert [r["address"] for r in milestones.list_milestones(
        db_session, anchor=REPO, in_line=True, status="planned")] == [_addr("v3")]


def test_merge_clears_tombstone_rank_and_inherits_when_into_unranked(db_session):
    _seed(db_session, "a", "b", "c", "d")
    for n in ("a", "b", "c"):
        milestones.place_in_line(db_session, _addr(n))
    b_rank = milestones.get(db_session, _addr("b")).line_rank
    # `into` unranked + same anchor → inherits `from`'s place.
    milestones.merge(db_session, _addr("b"), _addr("d"))
    assert milestones.get(db_session, _addr("b")).line_rank is None
    assert milestones.get(db_session, _addr("d")).line_rank == b_rank
    assert _line(db_session) == [_addr("a"), _addr("d"), _addr("c")]
    # `into` already ranked → keeps its own; tombstone cleared.
    milestones.merge(db_session, _addr("a"), _addr("c"))
    assert milestones.get(db_session, _addr("a")).line_rank is None
    assert _line(db_session) == [_addr("d"), _addr("c")]
    # Cross-anchor `into` never inherits.
    _seed(db_session, "x", anchor="acme/other")
    milestones.merge(db_session, _addr("d"), "acme/other/x")
    assert milestones.get(db_session, "acme/other/x").line_rank is None
    assert _line(db_session) == [_addr("c")]


def test_in_line_listing_is_exact_anchor_not_subtree(db_session):
    _seed(db_session, "org-rel", "org-unranked", anchor="acme")
    _seed(db_session, "repo-rel")
    milestones.place_in_line(db_session, "acme/org-rel")
    milestones.place_in_line(db_session, _addr("repo-rel"))
    assert _line(db_session, "acme") == ["acme/org-rel"]
    assert _line(db_session, REPO) == [_addr("repo-rel")]
    # The ordinary listing still subtree-filters.
    assert len(milestones.list_milestones(db_session, anchor="acme")) == 3
    assert milestones.list_milestones(db_session, anchor="nope", in_line=True) == []
    with pytest.raises(milestones.InvalidMilestoneFieldError):
        milestones.list_milestones(db_session, in_line=True)


def test_equal_ranks_tiebreak_by_address(db_session):
    """Two peers placing into the same gap concurrently can mint equal ranks;
    `address` breaks the tie deterministically, and the next placement INTO that
    tie still succeeds (rebalance)."""
    _seed(db_session, "zeta", "alpha", "mid")
    for n in ("zeta", "alpha"):
        db_session.execute(
            sa.update(Milestone)
            .where(Milestone.name == n)
            .values(line_rank=Decimal("1.5"))
        )
    db_session.commit()
    assert _line(db_session) == [_addr("alpha"), _addr("zeta")]
    out = milestones.place_in_line(db_session, _addr("mid"), after=_addr("alpha"))
    assert out["line"] == [_addr("alpha"), _addr("mid"), _addr("zeta")]


def test_rebalance_when_precision_exhausted(db_session):
    _seed(db_session, "lo", "hi", "x")
    # 1 and 1 + 1e-39: the midpoint needs 41 significant digits (> PRECISION).
    for n, r in (("lo", "1"), ("hi", "1." + "0" * 38 + "1")):
        db_session.execute(
            sa.update(Milestone).where(Milestone.name == n).values(line_rank=Decimal(r))
        )
    db_session.commit()
    out = milestones.place_in_line(db_session, _addr("x"), after=_addr("lo"))
    assert out["line"] == [_addr("lo"), _addr("x"), _addr("hi")]


def test_line_is_reserved_name(db_session):
    _seed(db_session)
    assert "line" in milestones.RESERVED_NAMES
    with pytest.raises(milestones.InvalidMilestoneNameError, match="reserved"):
        milestones.create(db_session, REPO, "line")
    with pytest.raises(milestones.InvalidMilestoneNameError, match="reserved"):
        milestones.create(db_session, REPO, "LINE")


def test_fracrank_wire_round_trip():
    assert fracrank.to_wire(Decimal("10")) == "10"
    assert fracrank.to_wire(Decimal("1.50")) == "1.5"
    assert fracrank.to_wire(Decimal("1E-7")) == "0.0000001"
    assert fracrank.to_wire(None) is None
    assert fracrank.from_wire("2.25") == Decimal("2.25")
    with pytest.raises(ValueError):
        fracrank.from_wire("nan")


# --- replication ----------------------------------------------------------------


def _subscribe(session):
    emit.create_outbound_subscription(
        session,
        "http://peer/replication/events/ingest",
        "topsecret",
        list(replication.MILESTONE_EVENTS),
        epoch="e1",
        source_id="hub.platform",
    )


def test_placement_and_merge_emit_updated_with_line_rank(db_session):
    _subscribe(db_session)
    _seed(db_session, "a", "b")
    milestones.place_in_line(db_session, _addr("a"))
    milestones.remove_from_line(db_session, _addr("b"))  # no-op: no event
    db_session.commit()
    milestones.merge(db_session, _addr("a"), _addr("b"))
    db_session.commit()
    rows = db_session.scalars(
        select(ReplicationOutboxRow).order_by(ReplicationOutboxRow.seq)
    ).all()
    tail = [(r.payload["event_type"], r.payload["payload"]) for r in rows][2:]
    assert [k for k, _ in tail] == [
        EVENT_MILESTONE_UPDATED, EVENT_MILESTONE_MERGED, EVENT_MILESTONE_UPDATED,
    ]
    placed = tail[0][1]
    assert placed["address"] == _addr("a") and placed["line_rank"] == "1"
    inherited = tail[2][1]
    assert inherited["address"] == _addr("b") and inherited["line_rank"] == "1"
    # Every created payload carries the key too (null when unranked).
    assert rows[0].payload["payload"]["line_rank"] is None


def test_apply_without_line_rank_key_preserves_local_rank(db_session):
    """Old-peer safety (release-line.md §2.3): a full-row payload WITHOUT the
    `line_rank` key preserves the local value; only an explicit null clears."""
    _seed(db_session, "v1")
    secret = _register(db_session)["secret"]
    milestones.place_in_line(db_session, _addr("v1"))
    db_session.commit()
    rank = milestones.get(db_session, _addr("v1")).line_rank
    assert rank is not None

    old_peer = _m_payload(REPO, "v1", outcome="edited by an old peer",
                          authored_at=T0 + timedelta(days=3650))
    assert "line_rank" not in old_peer
    status, resp = _deliver(db_session, secret, EVENT_MILESTONE_UPDATED, old_peer, 1)
    assert (status, resp["status"]) == (200, "applied")
    m = milestones.get(db_session, _addr("v1"))
    assert m.outcome == "edited by an old peer" and m.line_rank == rank

    moved = {**_m_payload(REPO, "v1", authored_at=T0 + timedelta(days=3651)),
             "line_rank": "7.5"}
    _deliver(db_session, secret, EVENT_MILESTONE_UPDATED, moved, 2)
    assert milestones.get(db_session, _addr("v1")).line_rank == Decimal("7.5")

    cleared = {**_m_payload(REPO, "v1", authored_at=T0 + timedelta(days=3652)),
               "line_rank": None}
    _deliver(db_session, secret, EVENT_MILESTONE_UPDATED, cleared, 3)
    assert milestones.get(db_session, _addr("v1")).line_rank is None


# --- HTTP ---------------------------------------------------------------------


class _AlwaysTrust:
    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _client() -> TestClient:
    return TestClient(
        create_app(resolver=TrustResolver([_AlwaysTrust()]), migrate_on_startup=False)
    )


def test_line_routes_round_trip(clean_db):
    with session_scope() as s:
        _seed(s, "v1", "v2", "v3")
        _seed(s, "org-rel", anchor="acme")
    c = _client()
    assert c.post(f"/milestones/{_addr('v1')}/line").status_code == 200
    r = c.post(f"/milestones/{_addr('v3')}/line", json={})
    assert r.json()["line"] == [_addr("v1"), _addr("v3")]
    r = c.post(f"/milestones/{_addr('v2')}/line", json={"after": "v1"})
    assert r.status_code == 200, r.text
    assert r.json()["line"] == [_addr("v1"), _addr("v2"), _addr("v3")]
    assert isinstance(r.json()["line_rank"], str)

    r = c.get("/milestones", params={"anchor": REPO, "in_line": "true"})
    assert [m["address"] for m in r.json()["milestones"]] == [
        _addr("v1"), _addr("v2"), _addr("v3")
    ]
    # Exact anchor: the org's line does not include repo members.
    r = c.get("/milestones", params={"anchor": "acme", "in_line": "true"})
    assert r.json()["milestones"] == []
    assert c.get("/milestones", params={"in_line": "true"}).status_code == 422

    # Errors: cross-anchor → 409, both neighbours → 422, unknown → 404.
    c.post("/milestones/acme/org-rel/line")
    r = c.post(f"/milestones/{_addr('v1')}/line", json={"after": "acme/org-rel"})
    assert r.status_code == 409
    r = c.post(f"/milestones/{_addr('v1')}/line", json={"after": "v2", "before": "v3"})
    assert r.status_code == 422
    assert c.post(f"/milestones/{_addr('nope')}/line").status_code == 404
    assert c.post(f"/milestones/{_addr('v1')}/line", json={"bogus": 1}).status_code == 422

    r = c.delete(f"/milestones/{_addr('v2')}/line")
    assert r.status_code == 200 and r.json()["line_rank"] is None
    assert r.json()["line"] == [_addr("v1"), _addr("v3")]
    # The bare GET still reads the row (the suffix route did not shadow it).
    assert c.get(f"/milestones/{_addr('v1')}").json()["line_rank"] is not None


# --- migration ------------------------------------------------------------------

_PREV = "a7b9c1d3e5f8"
_THIS = "b8c0d2e4f6a8"


@pytest.fixture()
def scratch_db(migrated_db):
    """A private database for migration up/down, so the shared test DB never
    leaves head mid-session."""
    url = sa.make_url(migrated_db)
    scratch = url.set(database=f"{url.database}_linerank_mig")
    maint = sa.create_engine(
        url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    name = scratch.database
    with maint.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    scratch_url = scratch.render_as_string(hide_password=False)
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(milestones.__file__), "migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", scratch_url)
    prior = os.environ["SNOWLINE_PLATFORM_DATABASE_URL"]
    os.environ["SNOWLINE_PLATFORM_DATABASE_URL"] = scratch_url
    try:
        yield cfg, sa.create_engine(scratch_url)
    finally:
        os.environ["SNOWLINE_PLATFORM_DATABASE_URL"] = prior
        with maint.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        maint.dispose()


def _cols(engine):
    return {c["name"] for c in sa.inspect(engine).get_columns("milestones")}


def test_migration_up_down_and_fails_loudly_on_existing_line(scratch_db):
    cfg, engine = scratch_db
    command.upgrade(cfg, _PREV)
    assert "line_rank" not in _cols(engine)
    with engine.begin() as conn:
        sid = conn.execute(
            sa.text(
                "INSERT INTO scopes (id, slug, name, kind, status, isolated) "
                "VALUES (gen_random_uuid(), 'acme', 'Acme', 'org', 'active', false) "
                "RETURNING id"
            )
        ).scalar()
        conn.execute(
            sa.text(
                "INSERT INTO milestones (id, anchor_scope_id, name, status) "
                "VALUES (gen_random_uuid(), :s, 'line', 'planned')"
            ),
            {"s": sid},
        )
    with pytest.raises(RuntimeError, match="acme/line"):
        command.upgrade(cfg, _THIS)
    engine.dispose()
    assert "line_rank" not in _cols(engine)

    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE milestones SET name = 'line-old'"))
    command.upgrade(cfg, _THIS)
    assert "line_rank" in _cols(engine)
    command.downgrade(cfg, _PREV)
    engine.dispose()
    assert "line_rank" not in _cols(engine)
    engine.dispose()
