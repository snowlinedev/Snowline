"""The §7.1 cold-start union merge (replication-continuity §7.1, issue #98) —
platform half.

Two layers:

  * SCOPE APPLY semantics on one store, with hand-built envelopes on a
    `#backfill` stream vs a live stream: created-collision CONVERGENCE by slug
    (no park, local id kept, WARNING), the scope LWW clock (the additive
    `created_at`/`updated_at` payload stamps; the winning event's stamp is
    persisted, never import time), parent re-resolution/healing by slug, the
    live `scope.updated`-by-slug change, and the parent-first `scope.created`-
    only enumerator.
  * The END-TO-END union between two real platform stores (the
    `platform_stores` two-instance harness) plus one plugin-shaped participant
    per side, driven through `replication_union.union` over the real admin
    surface: pair-first, scopes both directions to completion, plugins both
    directions, verify + retire — and the §7.1 acceptance list (no write lost,
    shared-slug pairs converge with no park and congruent parent chains, a
    fresh-epoch re-run changes nothing, one live stream per direction after).
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from snowline_plugin_sdk.contract import EVENT_SCOPE_CREATED, EVENT_SCOPE_UPDATED
from snowline_plugin_sdk.replication import emit, ingest
from snowline_plugin_sdk.replication.envelope import build_envelope, sign_body
from sqlalchemy import select

from snowline_platform import (
    milestones,
    replication,
    replication_pairing,
    replication_union,
    scopes,
)
from snowline_platform.models import (
    Milestone,
    MilestoneDependency,
    MilestoneTransition,
    Scope,
)

from ._replication_helpers import (
    RoutedClient,
    make_participant,
    make_platform,
    plugin_entry,
)

BF_STREAM = ("roam.platform#backfill", "bf-1")
LIVE_STREAM = ("roam.platform", "e-1")
T0 = datetime(2026, 7, 4, 12, 0, 0)


def _t(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _payload(slug, name, kind, *, parent=None, isolated=False, status="active",
             scope_id=None, created=None, updated=None):
    return {
        "id": str(scope_id or uuid.uuid4()),
        "slug": slug,
        "name": name,
        "kind": kind,
        "parent": parent,
        "isolated": isolated,
        "status": status,
        "created_at": created.isoformat() if created else None,
        "updated_at": updated.isoformat() if updated else None,
    }


class _Stream:
    """A registered inbound stream on the test store + a seq counter."""

    def __init__(self, session, stream):
        self.session = session
        self.stream = stream
        self.secret = ingest.register_inbound_stream(session, *stream)["secret"]
        session.commit()
        self.seq = 0

    def deliver(self, event_type, payload):
        self.seq += 1
        env = build_envelope(event_type, payload, source_id=self.stream[0],
                             epoch=self.stream[1], seq=self.seq, peer_seen=0)
        body = json.dumps(env).encode()
        out = ingest.ingest_delivery(
            self.session, body, f"sha256={sign_body(self.secret, body)}",
            scopes.apply_scope_event, park_after=2,
        )
        self.session.commit()
        return out


def _make_local(session, slug, name, kind, *, at, parent=scopes._UNSET):
    kw = {} if parent is scopes._UNSET else {"parent": parent}
    sc = scopes.create(session, slug=slug, name=name, kind=kind, **kw)
    sc.created_at = sc.updated_at = at
    session.commit()
    return sc


# --- payload stamps + the LWW coordinate ------------------------------------------


def test_payload_carries_additive_authored_and_updated_stamps(db_session):
    org = scopes.create(db_session, slug="acme", name="Acme", kind="org")
    db_session.commit()
    p = scopes.to_replication_payload(org)
    assert p["created_at"] == org.created_at.isoformat()
    assert p["updated_at"] == org.updated_at.isoformat()
    # The pre-§7.1 keys are all still there (additive only).
    assert {"id", "slug", "name", "kind", "parent", "isolated", "status"} <= set(p)


def test_local_update_stamp_is_explicit_and_monotonic(db_session):
    """`update` sets the LWW coordinate explicitly (suppressing onupdate=now())
    and strictly past the current stamp — even a peer's future-dated one."""
    org = scopes.create(db_session, slug="acme", name="Acme", kind="org")
    future = datetime(2099, 1, 1)
    org.updated_at = future
    db_session.commit()
    scopes.update(db_session, "acme", name="Acme 2")
    db_session.commit()
    assert scopes.resolve(db_session, "acme").updated_at == future + timedelta(microseconds=1)


def test_apply_persists_the_winning_events_stamp_not_import_time(db_session):
    """The replicated row's coordinate is the AUTHOR's — a live create lands
    with the payload's stamps, and a winning update persists its own stamp."""
    stream = _Stream(db_session, LIVE_STREAM)
    p = _payload("acme", "Acme", "org", created=_t(1), updated=_t(1))
    assert stream.deliver(EVENT_SCOPE_CREATED, p)[1]["status"] == "applied"
    row = scopes.resolve(db_session, "acme")
    assert (row.created_at, row.updated_at) == (_t(1), _t(1))

    stream.deliver(EVENT_SCOPE_UPDATED, {**p, "name": "Acme v2", "updated_at": _t(5).isoformat()})
    row = scopes.resolve(db_session, "acme")
    assert (row.name, row.updated_at) == ("Acme v2", _t(5))


# --- live scope.updated applies BY SLUG -----------------------------------------------


def test_live_update_with_a_foreign_wire_id_applies_by_slug_no_park(db_session):
    """§7.1: after a union, ids are permanently instance-local — a live update
    whose wire id differs from the local row's must apply by slug, not park."""
    local = _make_local(db_session, "acme", "Acme", "org", at=_t(1))
    stream = _Stream(db_session, LIVE_STREAM)
    p = _payload("acme", "Acme renamed", "org", created=_t(0), updated=_t(9))
    assert p["id"] != str(local.id)
    status, resp = stream.deliver(EVENT_SCOPE_UPDATED, p)
    assert (status, resp["status"]) == (200, "applied")
    row = scopes.resolve(db_session, "acme")
    assert (row.id, row.name, row.updated_at) == (local.id, "Acme renamed", _t(9))
    assert ingest.list_parked(db_session) == []


def test_live_update_older_than_local_state_loses(db_session):
    """LWW by the updated stamp: an older backlogged update can't clobber a
    newer local write (and the local coordinate stays)."""
    _make_local(db_session, "acme", "Newer local", "org", at=_t(10))
    stream = _Stream(db_session, LIVE_STREAM)
    stream.deliver(EVENT_SCOPE_UPDATED, _payload("acme", "older", "org", updated=_t(3)))
    row = scopes.resolve(db_session, "acme")
    assert (row.name, row.updated_at) == ("Newer local", _t(10))


def test_live_update_without_stamps_applies_as_before(db_session):
    """A pre-§7.1 peer's payload (no stamps) still applies unconditionally."""
    _make_local(db_session, "acme", "Acme", "org", at=_t(10))
    stream = _Stream(db_session, LIVE_STREAM)
    p = _payload("acme", "legacy update", "org")
    del p["created_at"], p["updated_at"]
    stream.deliver(EVENT_SCOPE_UPDATED, p)
    assert scopes.resolve(db_session, "acme").name == "legacy update"


def test_live_update_for_an_unknown_slug_is_retryable(db_session):
    stream = _Stream(db_session, LIVE_STREAM)
    status, resp = stream.deliver(EVENT_SCOPE_UPDATED, _payload("acme", "A", "org", updated=_t(1)))
    assert (status, resp["reason"]) == (503, "apply_failed")


def test_live_created_same_new_slug_race_still_parks(db_session):
    """§7.1 NARROWS the park posture to exactly this case — it is unchanged."""
    _make_local(db_session, "acme", "Acme", "org", at=_t(1))
    stream = _Stream(db_session, LIVE_STREAM)
    status, resp = stream.deliver(EVENT_SCOPE_CREATED, _payload("acme", "Peer", "org", updated=_t(2)))
    assert (status, resp["status"]) == (200, "parked")


# --- backfill scope.created converges by slug ---------------------------------------


def test_backfill_created_collision_converges_no_park(db_session, caplog):
    local = _make_local(db_session, "acme", "Local Acme", "org", at=_t(1))
    stream = _Stream(db_session, BF_STREAM)
    peer = _payload("acme", "Peer Acme (newer)", "org", created=_t(0), updated=_t(5))
    with caplog.at_level(logging.WARNING, logger="snowline_platform.scopes"):
        status, resp = stream.deliver(EVENT_SCOPE_CREATED, peer)
    assert (status, resp["status"]) == (200, "applied")
    assert ingest.list_parked(db_session) == []
    row = scopes.resolve(db_session, "acme")
    assert row.id == local.id  # local row and id kept; peer id advisory
    assert (row.name, row.updated_at) == ("Peer Acme (newer)", _t(5))  # LWW
    assert any("converged by slug" in r.getMessage() for r in caplog.records)
    assert (resp["applied_seq"], resp["gate_seq"]) == (1, 1)  # frontier flows


def test_backfill_created_older_peer_state_keeps_local_metadata(db_session):
    _make_local(db_session, "acme", "Local newer", "org", at=_t(10))
    stream = _Stream(db_session, BF_STREAM)
    stream.deliver(EVENT_SCOPE_CREATED, _payload("acme", "peer older", "org", updated=_t(2)))
    row = scopes.resolve(db_session, "acme")
    assert (row.name, row.updated_at) == ("Local newer", _t(10))


def test_backfill_created_heals_a_missing_parent_link_by_slug(db_session):
    """A cold-start side created `acme/w/deep` before `acme/w` existed (no
    parent link). The peer's payload names the parent by slug — convergence
    re-resolves it LOCALLY and heals the link, even when the local metadata
    wins LWW."""
    _make_local(db_session, "acme", "Acme", "org", at=_t(0))
    deep = _make_local(db_session, "acme/w/deep", "Deep (local newer)", "project",
                       at=_t(10), parent=None)
    w = _make_local(db_session, "acme/w", "W", "project", at=_t(11))
    assert deep.parent_id is None
    stream = _Stream(db_session, BF_STREAM)
    stream.deliver(EVENT_SCOPE_CREATED,
                   _payload("acme/w/deep", "Deep (peer older)", "project",
                            parent="acme/w", updated=_t(3)))
    row = scopes.resolve(db_session, "acme/w/deep")
    assert row.parent_id == w.id  # healed by slug, to the LOCAL row
    assert row.name == "Deep (local newer)"  # metadata LWW unaffected


def test_backfill_created_null_parent_never_unlinks_a_local_link(db_session):
    _make_local(db_session, "acme", "Acme", "org", at=_t(0))
    w = _make_local(db_session, "acme/w", "W", "project", at=_t(0))
    _make_local(db_session, "acme/w/deep", "Deep", "project", at=_t(1), parent="acme/w")
    stream = _Stream(db_session, BF_STREAM)
    stream.deliver(EVENT_SCOPE_CREATED,
                   _payload("acme/w/deep", "Deep newer", "project", parent=None, updated=_t(9)))
    row = scopes.resolve(db_session, "acme/w/deep")
    assert (row.name, row.parent_id) == ("Deep newer", w.id)


def test_backfill_created_for_an_unknown_slug_creates_with_the_peer_id(db_session):
    stream = _Stream(db_session, BF_STREAM)
    p = _payload("globex", "Globex", "org", created=_t(1), updated=_t(2))
    stream.deliver(EVENT_SCOPE_CREATED, p)
    row = scopes.resolve(db_session, "globex")
    assert (str(row.id), row.created_at, row.updated_at) == (p["id"], _t(1), _t(2))


def test_backfill_replay_is_idempotent(db_session):
    _make_local(db_session, "acme", "Acme", "org", at=_t(1))
    stream = _Stream(db_session, BF_STREAM)
    p = _payload("acme", "Peer", "org", updated=_t(5))
    stream.deliver(EVENT_SCOPE_CREATED, p)
    before = scopes.to_replication_payload(scopes.resolve(db_session, "acme"))
    scopes.apply_scope_event(db_session, build_envelope(
        EVENT_SCOPE_CREATED, p, source_id=BF_STREAM[0], epoch="bf-2", seq=1, peer_seen=0))
    db_session.commit()
    assert scopes.to_replication_payload(scopes.resolve(db_session, "acme")) == before


# --- the enumerator --------------------------------------------------------------------


def test_enumerator_is_created_only_parent_first_and_final_state(db_session):
    _make_local(db_session, "acme", "Acme", "org", at=_t(5))
    # A child CREATED before its parent row existed, later re-linked.
    _make_local(db_session, "acme/w/deep", "Deep", "project", at=_t(1), parent=None)
    _make_local(db_session, "acme/w", "W", "project", at=_t(3))
    scopes.update(db_session, "acme/w/deep", parent="acme/w", isolated=True)
    db_session.commit()

    events = list(scopes.enumerate_backfill(db_session))
    assert {e for e, _ in events} == {EVENT_SCOPE_CREATED}
    order = [p["slug"] for _, p in events]
    assert order.index("acme/w") < order.index("acme/w/deep")  # parent first
    deep = next(p for _, p in events if p["slug"] == "acme/w/deep")
    assert (deep["parent"], deep["isolated"]) == ("acme/w", True)  # FINAL state
    assert deep["updated_at"] and deep["created_at"]


# --- end-to-end: two platform stores + a plugin, through the union verb ------------------


def _toy_plugin(source_id: str):
    """A plugin-shaped participant whose domain is a tiny LWW register map — its
    apply + §7.1 enumerator are what a real plugin supplies."""
    rows: dict[str, dict] = {}

    def apply(session, envelope):
        p = envelope["payload"]
        cur = rows.get(p["id"])
        if cur is None or (p["at"], p["src"]) > (cur["at"], cur["src"]):
            rows[p["id"]] = dict(p)

    def enumerator(session):
        for rid in sorted(rows, key=lambda r: (rows[r]["at"], r)):
            yield ("toy.recorded", dict(rows[rid]))

    inst = make_participant(apply=apply, enumerator=enumerator)
    inst.rows = rows
    inst.source_id = source_id
    return inst


def _platform_instance(maker, host: str, toy_host: str):
    @contextmanager
    def scope():
        s = maker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = FastAPI()
    app.include_router(replication.build_router(scope))
    inst = SimpleNamespace(app=app, scope=scope, sessionmaker=maker)
    make_platform(
        plugins=[plugin_entry("toy", f"http://{toy_host}", events=["toy.recorded"])],
        inst=inst,
    )
    return inst


class _Pair:
    """The hub (`mini`) and spoke (`roam`) — each a real platform store plus a
    toy plugin — mounted on one RoutedClient."""

    def __init__(self, stores):
        self.hub = _platform_instance(stores["union_a"], "hub", "hub-toy")
        self.spoke = _platform_instance(stores["union_b"], "spoke", "spoke-toy")
        self.hub_toy = _toy_plugin("mini.toy")
        self.spoke_toy = _toy_plugin("roam.toy")
        self.client = RoutedClient({
            "hub": self.hub.app, "spoke": self.spoke.app,
            "hub-toy": self.hub_toy.app, "spoke-toy": self.spoke_toy.app,
        })

    def discover(self):
        local = replication_pairing.discover_participants(
            self.client, "http://hub", "mini", public_url="http://hub")
        peer = replication_pairing.discover_participants(
            self.client, "http://spoke", "roam", peer=True)
        return local, peer

    def drive(self):
        """One delivery pass on every store — what the four delivery loops do."""
        for maker in (self.hub.sessionmaker, self.spoke.sessionmaker,
                      self.hub_toy.sessionmaker, self.spoke_toy.sessionmaker):
            with maker() as s:
                emit.deliver_pending(s, self.client, reachability={})
                s.commit()

    def union(self, **kw):
        local, peer = self.discover()
        return replication_union.union(
            self.client, local, peer, drive=self.drive, timeout=30, report=lambda _m: None, **kw)

    def scopes_of(self, inst) -> dict:
        with inst.sessionmaker() as s:
            return {
                sc.slug: (sc.id, sc.name, sc.kind, sc.parent.slug if sc.parent else None,
                          sc.isolated, sc.status, sc.updated_at)
                for sc in s.scalars(select(Scope))
            }

    def parked(self, inst):
        with inst.sessionmaker() as s:
            return ingest.list_parked(s)


def _write(inst, fn):
    with inst.sessionmaker() as s:
        fn(s)
        s.commit()


def _seed_divergent_history(pair: _Pair):
    """mini: months of history. roam: stood up EMPTY, re-created the shared
    slugs under its own ids, created `acme/widget/deep` before `acme/widget`."""
    def hub(s):
        for slug, name, kind, at, parent in (
            ("acme", "Acme", "org", _t(1), scopes._UNSET),
            ("acme/widget", "Widget (hub, older)", "project", _t(2), scopes._UNSET),
            ("acme/widget/deep", "Deep", "project", _t(3), scopes._UNSET),
            ("acme/hub-only", "Hub only", "project", _t(4), scopes._UNSET),
            ("globex", "Globex", "org", _t(5), scopes._UNSET),
        ):
            kw = {} if parent is scopes._UNSET else {"parent": parent}
            sc = scopes.create(s, slug=slug, name=name, kind=kind, **kw)
            sc.created_at = sc.updated_at = at
            s.flush()

    def spoke(s):
        for slug, name, kind, at in (
            ("acme", "Acme", "org", _t(100)),
            ("acme/widget/deep", "Deep", "project", _t(101)),  # parent not there yet
            ("acme/widget", "Widget (roam, newer)", "project", _t(102)),
            ("acme/roam-only", "Roam only", "project", _t(103)),
        ):
            sc = scopes.create(s, slug=slug, name=name, kind=kind)
            sc.created_at = sc.updated_at = at
            s.flush()

    _write(pair.hub, hub)
    _write(pair.spoke, spoke)
    pair.hub_toy.rows["h1"] = {"id": "h1", "val": "hub", "at": "2026-07-01", "src": "mini.toy"}
    pair.hub_toy.rows["x"] = {"id": "x", "val": "hub-old", "at": "2026-07-02", "src": "mini.toy"}
    pair.spoke_toy.rows["s1"] = {"id": "s1", "val": "roam", "at": "2026-07-03", "src": "roam.toy"}
    pair.spoke_toy.rows["x"] = {"id": "x", "val": "roam-new", "at": "2026-07-04", "src": "roam.toy"}


@pytest.fixture()
def union_pair(platform_stores):
    return _Pair(platform_stores)


def test_union_converges_two_divergent_instances(union_pair):
    pair = union_pair
    _seed_divergent_history(pair)
    hub_ids = {k: v[0] for k, v in pair.scopes_of(pair.hub).items()}
    spoke_ids = {k: v[0] for k, v in pair.scopes_of(pair.spoke).items()}
    assert spoke_ids["acme/widget/deep"] and pair.scopes_of(pair.spoke)["acme/widget/deep"][3] is None

    result = pair.union()
    assert result.paired == "paired" and result.skipped == []

    hub, spoke = pair.scopes_of(pair.hub), pair.scopes_of(pair.spoke)
    # No write lost: the slug UNION on both sides.
    assert set(hub) == set(spoke) == {
        "acme", "acme/widget", "acme/widget/deep", "acme/hub-only", "acme/roam-only", "globex",
    }
    # Shared slugs converge to ONE local row per side, each keeping its own id.
    for slug in ("acme", "acme/widget", "acme/widget/deep"):
        assert hub[slug][0] == hub_ids[slug] and spoke[slug][0] == spoke_ids[slug]
    # Congruent parent chains and identical metadata (LWW by updated stamp).
    for slug in hub:
        assert hub[slug][1:] == spoke[slug][1:], slug
    assert hub["acme/widget"][1] == "Widget (roam, newer)"
    assert spoke["acme/widget/deep"][3] == "acme/widget"  # healed from the peer
    # Nothing parked anywhere.
    assert pair.parked(pair.hub) == [] and pair.parked(pair.spoke) == []
    # The plugin backfilled both ways too — union + same-object LWW.
    for toy in (pair.hub_toy, pair.spoke_toy):
        assert set(toy.rows) == {"h1", "s1", "x"}
        assert toy.rows["x"]["val"] == "roam-new"



def test_after_retirement_one_live_stream_per_direction_remains(union_pair):
    pair = union_pair
    _seed_divergent_history(pair)
    pair.union()
    for host, mine, peer in (("hub", "mini", "roam"), ("spoke", "roam", "mini")):
        for admin, name in ((f"http://{host}/replication-admin", "platform"),
                            (f"http://{host}/via/toy/replication-admin", "toy")):
            outs = pair.client.get(f"{admin}/outbound").json()
            ins = pair.client.get(f"{admin}/inbound").json()
            assert [r["source_id"] for r in outs if r["active"]] == [f"{mine}.{name}"]
            assert [r["source_id"] for r in ins if r["active"]] == [f"{peer}.{name}"]
            # The backfill halves stay for audit, retired.
            assert {r["source_id"] for r in outs if not r["active"]} == {f"{mine}.{name}#backfill"}

    # Live replication still flows — and a post-union update to a SHARED slug
    # (different ids on the two sides) applies by slug, never parks.
    _write(pair.spoke, lambda s: scopes.update(s, "acme", name="Acme (renamed on roam)"))
    pair.drive()
    assert pair.scopes_of(pair.hub)["acme"][1] == "Acme (renamed on roam)"
    assert pair.parked(pair.hub) == []


def test_rerun_of_a_completed_union_under_fresh_epochs_changes_nothing(union_pair):
    pair = union_pair
    _seed_divergent_history(pair)
    pair.union()
    before = (pair.scopes_of(pair.hub), pair.scopes_of(pair.spoke),
              dict(pair.hub_toy.rows), dict(pair.spoke_toy.rows))
    result = pair.union()  # already paired → reused; fresh backfill epochs
    assert result.paired == "reused"
    assert all(not s.resumed for s in result.streams)
    after = (pair.scopes_of(pair.hub), pair.scopes_of(pair.spoke),
             dict(pair.hub_toy.rows), dict(pair.spoke_toy.rows))
    assert after == before


def test_one_sided_empty_store_degenerates_to_one_real_direction(union_pair):
    pair = union_pair
    _write(pair.hub, lambda s: scopes.create(s, slug="acme", name="Acme", kind="org"))
    result = pair.union()
    finals = {(s.participant, s.sender.source_id): s.final_seq for s in result.streams}
    assert finals[("platform", "roam.platform")] == 0  # the empty side
    assert finals[("platform", "mini.platform")] == 1
    assert set(pair.scopes_of(pair.spoke)) == {"acme"}


def test_repair_retires_both_halves_of_both_sides_before_pairing(union_pair):
    pair = union_pair
    local, peer = pair.discover()
    replication_pairing.pair(pair.client, local, peer, report=lambda _m: None)
    old = {s["epoch"] for s in pair.client.get("http://hub/replication-admin/outbound").json()}
    result = pair.union(repair=True)
    assert result.paired == "repaired"
    for host in ("hub", "spoke"):
        outs = pair.client.get(f"http://{host}/replication-admin/outbound").json()
        ins = pair.client.get(f"http://{host}/replication-admin/inbound").json()
        assert all(not r["active"] for r in outs + ins if r["epoch"] in old)
        assert len([r for r in outs if r["active"]]) == 1
        assert len([r for r in ins if r["active"]]) == 1


def test_partial_live_pairing_refuses_without_repair(union_pair):
    pair = union_pair
    local, peer = pair.discover()
    replication_pairing.handshake_direction(
        pair.client, local["platform"], peer["platform"])
    with pytest.raises(replication_union.UnionError, match="--repair"):
        pair.union()


def test_parked_backfill_event_stops_the_union_then_resumes(union_pair, monkeypatch):
    """A backfill event that parks STOPS the union loudly (never a skip); once
    the operator resolves it the re-run resumes the same open stream."""
    pair = union_pair
    _write(pair.hub, lambda s: scopes.create(s, slug="acme", name="Acme", kind="org"))

    real = scopes.apply_scope_event
    calls = {"n": 0}

    def flaky(session, envelope):
        if envelope["source"].endswith("#backfill") and calls["n"] == 0:
            calls["n"] += 1
            raise scopes.ParkNow("simulated poison")
        return real(session, envelope)

    monkeypatch.setattr(replication, "scopes", SimpleNamespace(
        apply_scope_event=flaky, enumerate_backfill=scopes.enumerate_backfill))
    with pytest.raises(replication_union.UnionError, match="PARKED"):
        pair.union()
    parked = pair.parked(pair.spoke)
    assert [p["source_id"] for p in parked] == ["mini.platform#backfill"]

    # Operator resolves: re-apply through the admin verb, then re-run.
    p = parked[0]
    resp = pair.client.post("http://spoke/replication-admin/parked/reapply",
                            json={"source_id": p["source_id"], "epoch": p["epoch"], "seq": p["seq"]})
    assert resp.status_code == 200, resp.text
    result = pair.union()
    assert any(s.resumed for s in result.streams)
    assert set(pair.scopes_of(pair.spoke)) == {"acme"}


def test_participant_without_an_enumerator_is_skipped_loudly(union_pair):
    pair = union_pair
    pair.spoke_toy = None  # replaced below by an enumerator-less participant
    bare = make_participant()
    pair.client = RoutedClient({
        "hub": pair.hub.app, "spoke": pair.spoke.app,
        "hub-toy": pair.hub_toy.app, "spoke-toy": bare.app,
    })
    pair.spoke_toy = bare
    pair.spoke_toy.rows = {}
    result = pair.union()
    assert result.skipped == ["toy:roam.toy"]
    # The receiver-side half it registered was retired again — nothing dangles.
    ins = pair.client.get("http://hub/via/toy/replication-admin/inbound").json()
    assert not [r for r in ins if r["active"] and r["source_id"].endswith("#backfill")]


def test_drop_retired_parks_after_the_union_verifies(union_pair):
    """An old unresolved park on a pre-union stream is dropped once the union
    verifies (its payload re-arrived on the backfill stream)."""
    pair = union_pair
    with pair.hub.sessionmaker() as s:
        ingest.register_inbound_stream(s, "roam.platform", "old-epoch")
        s.commit()
        env = build_envelope(EVENT_SCOPE_CREATED, _payload("x", "X", "org"),
                             source_id="roam.platform", epoch="old-epoch", seq=1, peer_seen=0)
        s.add(ingest.ReplicationParkedEvent(
            source_id="roam.platform", epoch="old-epoch", seq=1,
            event_type=EVENT_SCOPE_CREATED, payload=env, reason="old park"))
        s.commit()
    # A previously-paired pair: --repair retires the old stream on both halves.
    result = pair.union(repair=True, drop_retired_parks=True)
    assert [(p["source_id"], p["seq"]) for p in result.dropped_parks] == [("roam.platform", 1)]
    assert pair.parked(pair.hub) == []


# --- milestones ride the platform stream too (review of #98, M1) ----------------


def _milestones_of(inst):
    """Every milestone row as (outcome, status, alias target, transition log),
    plus the dependency edge set — both by canonical address."""
    with inst.sessionmaker() as s:
        rows = {}
        for m in s.scalars(select(Milestone)):
            log = sorted(
                (t.from_status, t.to_status, t.authored_at)
                for t in s.scalars(
                    select(MilestoneTransition).where(MilestoneTransition.milestone_id == m.id)
                )
            )
            rows[milestones.address_of(m)] = (
                m.outcome, m.status,
                milestones.address_of(m.merged_into) if m.merged_into_id else None,
                log,
            )
        deps = {
            (
                milestones.address_of(s.get(Milestone, e.dependent_id)),
                milestones.address_of(s.get(Milestone, e.dependency_id)),
            )
            for e in s.scalars(select(MilestoneDependency))
        }
        return rows, deps


def _seed_milestone_history(pair: _Pair):
    """mini: `acme/v1` activated, `acme/v2` depending on it, `acme/old` merged
    into `v2`. roam (stood up empty): re-created `acme` and `acme/v1` with a
    NEWER outcome but an older lifecycle clock, plus its own `acme/v3`."""
    def hub(s):
        scopes.create(s, slug="acme", name="Acme", kind="org")
        milestones.create(s, "acme", "v1", outcome="hub outcome")
        milestones.activate(s, "acme/v1", reason="go")
        milestones.create(s, "acme", "v2", outcome="after v1")
        milestones.add_dependency(s, "acme/v2", "acme/v1")
        milestones.create(s, "acme", "old")
        milestones.merge(s, "acme/old", "acme/v2")

    def spoke(s):
        scopes.create(s, slug="acme", name="Acme", kind="org")
        v1 = milestones.create(s, "acme", "v1", outcome="roam outcome (newer)")
        v1.lww_authored_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)  # descriptive: roam wins
        v1.lifecycle_authored_at = _t(1)  # lifecycle: the hub's activation wins
        milestones.create(s, "acme", "v3", outcome="roam only")

    _write(pair.hub, hub)
    _write(pair.spoke, spoke)


def test_enumerator_walks_rows_then_transitions_then_edges_then_merges(union_pair):
    pair = union_pair
    _seed_milestone_history(pair)
    with pair.hub.sessionmaker() as s:
        events = list(milestones.enumerate_backfill(s))
    assert [(t, p.get("address") or p.get("dependent") or p.get("from")) for t, p in events] == [
        ("milestone.created", "acme/v1"),
        ("milestone.transitioned", "acme/v1"),
        ("milestone.created", "acme/v2"),
        ("milestone.created", "acme/old"),
        ("milestone.dependency_changed", "acme/v2"),
        ("milestone.merged", "acme/old"),
    ]
    created_v1, transitioned_v1 = events[0][1], events[1][1]
    # The log event carries the row's FINAL lifecycle clock (so it loses the
    # register compare on a peer the created event seeded) and its own instant.
    assert transitioned_v1["lifecycle_authored_at"] == created_v1["lifecycle_authored_at"]
    assert transitioned_v1["authored_at"] != created_v1["authored_at"]
    assert (transitioned_v1["from_status"], transitioned_v1["to_status"]) == ("planned", "active")
    # Scopes come first on the combined platform stream.
    with pair.hub.sessionmaker() as s:
        combined = [t for t, _ in replication.enumerate_backfill(s)]
    assert combined[0] == EVENT_SCOPE_CREATED and combined[1:] == [t for t, _ in events]


def test_union_carries_milestones_both_ways(union_pair):
    pair = union_pair
    _seed_milestone_history(pair)
    result = pair.union()
    assert result.skipped == []
    hub_rows, hub_deps = _milestones_of(pair.hub)
    spoke_rows, spoke_deps = _milestones_of(pair.spoke)
    # No write lost, both directions — and identical state on both sides.
    assert set(hub_rows) == set(spoke_rows) == {"acme/v1", "acme/v2", "acme/old", "acme/v3"}
    assert hub_rows == spoke_rows
    assert hub_deps == spoke_deps == {("acme/v2", "acme/v1")}
    # Same-milestone edit resolves PER REGISTER by LWW: roam's newer outcome,
    # the hub's newer lifecycle (active) — and the hub's transition log row
    # landed on roam.
    outcome, status, alias, log = spoke_rows["acme/v1"]
    assert (outcome, status, alias) == ("roam outcome (newer)", "active", None)
    assert [(f, t) for f, t, _ in log] == [("planned", "active")]
    assert spoke_rows["acme/old"][2] == "acme/v2"  # the tombstone followed
    assert hub_rows["acme/v3"][0] == "roam only"
    assert pair.parked(pair.hub) == [] and pair.parked(pair.spoke) == []
    # Re-running under fresh epochs changes nothing.
    pair.union()
    assert _milestones_of(pair.hub) == (hub_rows, hub_deps)
    assert _milestones_of(pair.spoke) == (spoke_rows, spoke_deps)


# --- mixed-version refusal (review of #98, M2) ------------------------------------


class _Pre98Client:
    """The real routed client, except one participant answers 404 on the §7.1
    backfill surface — an instance that predates #98."""

    def __init__(self, inner, missing_url: str):
        self._inner, self._missing = inner, missing_url

    def get(self, url, **kw):
        if url == self._missing:
            return httpx.Response(404, json={"detail": "Not Found"},
                                  request=httpx.Request("GET", url))
        return self._inner.get(url, **kw)

    def post(self, url, **kw):
        return self._inner.post(url, **kw)


def test_refuses_a_peer_without_the_backfill_surface(union_pair):
    pair = union_pair
    _write(pair.hub, lambda s: scopes.create(s, slug="acme", name="Acme", kind="org"))
    pair.client = _Pre98Client(pair.client, "http://spoke/replication-admin/backfill")
    with pytest.raises(replication_union.UnionError, match="upgrade the peer first"):
        pair.union()
    # Refused BEFORE step 1: nothing was paired, nothing opened, nothing parked.
    assert pair.client.get("http://spoke/replication-admin/inbound").json() == []
    assert pair.client.get("http://hub/replication-admin/inbound").json() == []
    assert set(pair.scopes_of(pair.spoke)) == set()
