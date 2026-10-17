"""The §7.1 cold-start union merge — governance half (replication-continuity
§7.1, issue #98).

Two complete governance stores (the `governance_stores` two-instance harness),
each with its OWN scope ids for the shared slugs (`StubScopeClient(id_salt=…)` —
two platforms that minted the same slugs independently), wired through httpx
transports that drive the receiver's SDK `ingest_delivery` with the real
`build_apply`. History is authored while UNPAIRED; the union then pairs the
live streams and backfills each direction with `enumerate_backfill` over a
`<source>#backfill` stream, verifies, and retires both halves.

Pins the §7.1 acceptance list for governance: no write lost (lifecycle state
included — supersession, revisions, retirement, archival, graduation);
same-object collisions LWW-resolve; cross-partition siblings flag ONCE and only
on CREATED rows (provenance-discriminated candidates, UUID no-ops flag
nothing); scope references resolve by slug to the LOCAL id; a fresh-epoch
re-run changes nothing; one live stream per direction after retirement.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from unittest import mock

import httpx
import pytest
from snowline_governance import artifacts, concurrence, decisions, graduation, shadow
from snowline_governance.contract import EVENT_TYPES
from snowline_governance.models import (
    Artifact,
    ArtifactVersion,
    Decision,
    DecisionConcurrence,
    LwwRegister,
    ShadowBranch,
    ShadowNode,
)
from snowline_governance.replication_apply import build_apply
from snowline_governance.replication_stream import enumerate_backfill
from snowline_plugin_sdk.replication import emit, ingest
from sqlalchemy import select

TREE = {
    "acme": None,
    "acme/widget": "acme",
    "acme/widget/deep": "acme/widget",
    "acme/other": "acme",
}


class Instance:
    """One complete governance store, its replication identity, and its OWN
    platform (a salted stub: same slugs, instance-local scope ids)."""

    def __init__(self, sessions, source_id: str, stub_factory):
        self.sessions = sessions
        self.source_id = source_id
        self.scopes = stub_factory(TREE, id_salt=source_id)
        self.apply = build_apply(self.scopes)

    @contextmanager
    def session(self):
        env = {"SNOWLINE_REPLICATION_SOURCE_ID": self.source_id}
        with mock.patch.dict(os.environ, env), self.sessions() as s:
            yield s
            s.commit()

    def sid(self, slug):
        return self.scopes.resolve(slug)["id"]

    def record(self, slug, text) -> str:
        with self.session() as s:
            return decisions.record_decision(s, slug, self.sid(slug), text)["id"]

    def supersede(self, prior, text) -> str:
        with self.session() as s:
            return decisions.supersede_decision(s, prior, text)["id"]

    def ingest_transport(self) -> httpx.BaseTransport:
        inst = self

        class _T(httpx.BaseTransport):
            def handle_request(self, request):
                if request.method == "GET":
                    return httpx.Response(405)
                s = inst.sessions()
                try:
                    status, body = ingest.ingest_delivery(
                        s, request.content, request.headers.get("X-Snowline-Signature"),
                        inst.apply,
                    )
                    s.commit()
                finally:
                    s.close()
                return httpx.Response(status, json=body)

        return _T()

    # --- reads --------------------------------------------------------------

    def decisions(self) -> dict:
        with self.sessions() as s:
            return {
                str(d.id): (d.scope_slug, d.decision,
                            str(d.supersedes_id) if d.supersedes_id else None)
                for d in s.scalars(select(Decision))
            }

    def pairs(self) -> set:
        with self.sessions() as s:
            return {
                frozenset({str(c.decision_id), str(c.concurrent_with_id)})
                for c in s.scalars(select(DecisionConcurrence))
            }

    def unreconciled(self) -> set:
        with self.sessions() as s:
            view = concurrence.unreconciled_pairs(s)
        return {frozenset(d["id"] for d in p["decisions"]) for p in view["pairs"]}

    def snapshot(self) -> dict:
        """Everything a re-run could touch, byte-for-byte."""
        with self.sessions() as s:
            return {
                "decisions": sorted(
                    (str(d.id), str(d.scope_id), d.scope_slug, d.decision,
                     str(d.supersedes_id), d.origin_source, d.recorded_at,
                     d.shadow_origin_node_id, d.shadow_origin_label)
                    for d in s.scalars(select(Decision))
                ),
                "pairs": sorted(
                    (str(c.decision_id), str(c.concurrent_with_id), c.marked_compatible_at)
                    for c in s.scalars(select(DecisionConcurrence))
                ),
                "artifacts": sorted(
                    (str(a.id), a.maturity, a.retirement_reason, a.superseded_at,
                     str(a.superseded_by_id), a.governs_all)
                    for a in s.scalars(select(Artifact))
                ),
                "versions": sorted(
                    (str(v.id), str(v.supersedes_id), v.status, v.body_snapshot)
                    for v in s.scalars(select(ArtifactVersion))
                ),
                "registers": sorted(
                    (r.object_kind, str(r.object_id), r.field, r.written_at,
                     r.source_id, r.event_ref)
                    for r in s.scalars(select(LwwRegister))
                ),
                "branches": sorted(
                    (str(b.id), b.status, b.archived_at, b.narrative_notes)
                    for b in s.scalars(select(ShadowBranch))
                ),
                "nodes": sorted(
                    (str(n.id), str(n.graduated_decision_id))
                    for n in s.scalars(select(ShadowNode))
                ),
            }


@pytest.fixture()
def hub_spoke(governance_stores, stub_scope_client):
    return (
        Instance(governance_stores["repl_a"], "mini.governance", stub_scope_client),
        Instance(governance_stores["repl_b"], "roam.governance", stub_scope_client),
    )


def _pair(a: Instance, b: Instance, tag: str = "1") -> None:
    """The live §5 handshake, both directions (receiver mints)."""
    for sender, receiver in ((a, b), (b, a)):
        epoch = f"live-{tag}-{sender.source_id}"
        with receiver.sessions() as s:
            secret = ingest.register_inbound_stream(s, sender.source_id, epoch)["secret"]
            s.commit()
        with sender.sessions() as s:
            emit.create_outbound_subscription(
                s, f"http://{receiver.source_id}/events/ingest", secret,
                sorted(EVENT_TYPES), epoch=epoch, source_id=sender.source_id,
                peer_source_id=receiver.source_id,
            )
            s.commit()


def _unpair(a: Instance, b: Instance) -> None:
    """A partition that outlives the pairing: every live stream retired on both
    halves of both sides (what `--repair` does before re-pairing)."""
    for inst in (a, b):
        with inst.sessions() as s:
            for sub in emit.list_outbound_subscriptions(s):
                if sub["active"]:
                    emit.retire_outbound_subscription(s, sub["id"])
            for st in ingest.list_inbound_streams(s):
                if st["active"]:
                    ingest.retire_inbound_stream(s, st["source_id"], st["epoch"])
            s.commit()


def _deliver(sender: Instance, receiver: Instance) -> int:
    total = 0
    with httpx.Client(transport=receiver.ingest_transport()) as client:
        while True:
            with sender.sessions() as s:
                n = emit.deliver_pending(s, client, reachability={})
                s.commit()
            total += n
            if n == 0:
                return total


def _sync(a, b):
    _deliver(a, b)
    _deliver(b, a)


def _backfill(sender: Instance, receiver: Instance, epoch: str) -> dict:
    bf = f"{sender.source_id}#backfill"
    with receiver.sessions() as s:
        secret = ingest.register_inbound_stream(s, bf, epoch)["secret"]
        s.commit()
    with sender.sessions() as s:
        opened = emit.open_backfill_subscription(
            s, f"http://{receiver.source_id}/events/ingest", secret,
            epoch=epoch, live_source_id=sender.source_id, enumerator=enumerate_backfill,
        )
        s.commit()
    _deliver(sender, receiver)
    with receiver.sessions() as s:
        stream = next(st for st in ingest.list_inbound_streams(s)
                      if st["source_id"] == bf and st["epoch"] == epoch)
        assert ingest.list_parked(s) == [], ingest.list_parked(s)
        assert stream["applied_seq"] == opened["final_seq"]
        ingest.retire_inbound_stream(s, bf, epoch)
        s.commit()
    with sender.sessions() as s:
        emit.retire_backfill_subscription(s, opened["id"])
        s.commit()
    return opened


def _union(a, b, tag="1"):
    _backfill(a, b, f"bf-{tag}-a")
    _backfill(b, a, f"bf-{tag}-b")


# --- acceptance --------------------------------------------------------------------


def _rich_history(hub: Instance, spoke: Instance) -> dict:
    """mini: months of history across every replicated surface. roam: stood up
    EMPTY, a week of its own."""
    ids = {}
    ids["h1"] = hub.record("acme/widget", "use postgres")
    ids["h2"] = hub.supersede(ids["h1"], "use postgres 17")  # lifecycle
    with hub.session() as s:
        art = artifacts.register_artifact(s, body="# spec v1")
        artifacts.revise_artifact(s, art["id"], "refines", body_snapshot="# spec v2")
        artifacts.set_maturity(s, art["id"], "stable")
        artifacts.set_code_refs(s, art["id"], [{"path": "src/x.py"}])
        old = artifacts.register_artifact(s, body="# retired checklist")
        artifacts.retire_artifact(s, old["id"], "completed")
        shadow.create_branch(s, "acme/widget", hub.sid("acme/widget"), "line-a")
        node = shadow.add_node(s, "acme/widget", "line-a", "spec it")
        shadow.add_message(s, shadow.get_branch(s, "acme/widget", "line-a")["id"], "hi", "agent")
    ids["art"], ids["old"], ids["node"] = art["id"], old["id"], node["id"]
    with hub.session() as s:
        ids["grad"] = graduation.graduate_node(s, node["id"])["decision_id"]
    with hub.session() as s:
        shadow.archive_branch(s, "acme/widget", "line-a")
    ids["r1"] = spoke.record("acme/other", "roam-side call")
    with spoke.session() as s:
        ids["sart"] = artifacts.register_artifact(s, body="# roam doc")["id"]
    return ids


def test_union_of_divergent_stores_loses_no_write(hub_spoke):
    hub, spoke = hub_spoke
    ids = _rich_history(hub, spoke)
    _pair(hub, spoke)
    _union(hub, spoke)

    assert hub.decisions() == spoke.decisions()
    assert {ids["h1"], ids["h2"], ids["grad"], ids["r1"]} <= set(hub.decisions())
    assert hub.decisions()[ids["h2"]][2] == ids["h1"]  # supersession edge travelled
    hub_snap, spoke_snap = hub.snapshot(), spoke.snapshot()
    for key in ("artifacts", "versions", "registers", "branches", "nodes"):
        assert hub_snap[key] == spoke_snap[key], key
    arts = {a[0]: a for a in spoke_snap["artifacts"]}
    assert arts[ids["art"]][1] == "stable"
    assert arts[ids["old"]][2] == "completed"  # retirement travelled
    assert ids["sart"] in {a[0] for a in hub_snap["artifacts"]}
    assert {b[1] for b in spoke_snap["branches"]} == {"archived"}
    assert dict(spoke_snap["nodes"])[ids["node"]] == ids["grad"]  # graduation pointer
    with spoke.sessions() as s:
        grad = s.get(Decision, uuid.UUID(ids["grad"]))
        assert grad.shadow_origin_label  # the graduation stamp's append half


def test_scope_references_resolve_by_slug_to_the_local_id(hub_spoke):
    """§7.1: the wire `scope_id` is advisory — every replicated row stores THIS
    instance's id for the slug (the two platforms minted different ids)."""
    hub, spoke = hub_spoke
    h = hub.record("acme/widget", "hub call")
    assert hub.sid("acme/widget") != spoke.sid("acme/widget")
    _pair(hub, spoke)
    _union(hub, spoke)
    with spoke.sessions() as s:
        row = s.get(Decision, uuid.UUID(h))
        assert str(row.scope_id) == spoke.sid("acme/widget")
        assert row.origin_source == "mini.governance"  # suffix stripped
    with hub.sessions() as s:
        assert s.get(Decision, uuid.UUID(h)).origin_source is None


def test_cross_partition_siblings_flag_once_and_only_on_created_rows(hub_spoke):
    hub, spoke = hub_spoke
    h1 = hub.record("acme/widget", "hub: widget rule")
    h2 = hub.record("acme", "hub: org rule")  # parent scope — chain overlap
    hub.record("acme/other", "hub: other rule")  # sibling scope — no overlap
    r1 = spoke.record("acme/widget/deep", "roam: deep rule")
    _pair(hub, spoke)
    _union(hub, spoke)

    expected = {frozenset({h1, r1}), frozenset({h2, r1})}
    # Flagged symmetrically, and ONLY cross-partition pairs: the hub's own
    # decisions, arriving on the spoke one after another, never flag against
    # each other (provenance makes the earlier ones non-candidates).
    assert hub.pairs() == expected
    assert spoke.pairs() == expected
    assert hub.unreconciled() == spoke.unreconciled() == expected

    # Re-running the union: every event is a UUID no-op → flags NOTHING new.
    _union(hub, spoke, "2")
    assert hub.pairs() == spoke.pairs() == expected


def test_previously_paired_reunion_same_object_collisions_lww_resolve(hub_spoke):
    """Shared pre-divergence rows, then a partition that outlived the pairing:
    the same artifact's maturity set on both sides LWW-resolves to the newer;
    the same decision superseded on both sides keeps BOTH superseders (append
    halves) and flags the new pair — while the shared corpus flags nothing."""
    hub, spoke = hub_spoke
    _pair(hub, spoke, "old")
    x = hub.record("acme/widget", "shared X")
    with hub.session() as s:
        art = artifacts.register_artifact(s, body="# shared")["id"]
    _sync(hub, spoke)
    _unpair(hub, spoke)

    a = hub.supersede(x, "A supersedes X (hub)")
    with hub.session() as s:
        artifacts.set_maturity(s, art, "draft")
    b = spoke.supersede(x, "B supersedes X (roam)")
    with spoke.session() as s:
        artifacts.set_maturity(s, art, "exploratory")  # later → wins

    _pair(hub, spoke, "new")
    _union(hub, spoke)

    for inst in (hub, spoke):
        decs = inst.decisions()
        assert decs[a][2] == x and decs[b][2] == x  # both append halves landed
        with inst.sessions() as s:
            assert s.get(Artifact, uuid.UUID(art)).maturity == "exploratory"
        assert inst.unreconciled() == {frozenset({a, b})}
    assert hub.snapshot()["registers"] == spoke.snapshot()["registers"]


def test_rerun_under_a_fresh_epoch_changes_nothing(hub_spoke):
    hub, spoke = hub_spoke
    _rich_history(hub, spoke)
    spoke.record("acme/widget", "roam widget call")
    _pair(hub, spoke)
    _union(hub, spoke, "1")
    before = (hub.snapshot(), spoke.snapshot())
    _union(hub, spoke, "2")
    assert (hub.snapshot(), spoke.snapshot()) == before


def test_after_retirement_one_live_stream_per_direction_remains(hub_spoke):
    hub, spoke = hub_spoke
    hub.record("acme/widget", "x")
    _pair(hub, spoke)
    _union(hub, spoke)
    for inst, peer in ((hub, spoke), (spoke, hub)):
        with inst.sessions() as s:
            outs = emit.list_outbound_subscriptions(s)
            ins = ingest.list_inbound_streams(s)
        assert [o["source_id"] for o in outs if o["active"]] == [inst.source_id]
        assert [i["source_id"] for i in ins if i["active"]] == [peer.source_id]
    # Post-union live traffic flags through the ordinary outbox mechanism.
    h = hub.record("acme/widget", "post-union hub")
    r = spoke.record("acme/widget", "post-union roam")
    _sync(hub, spoke)
    assert frozenset({h, r}) in hub.unreconciled()
    assert frozenset({h, r}) in spoke.unreconciled()


def test_enumerator_orders_supersession_parents_first_and_pins_original_times(hub_spoke):
    hub, _ = hub_spoke
    x = hub.record("acme/widget", "X")
    y = hub.supersede(x, "Y")
    with hub.sessions() as s:
        events = list(enumerate_backfill(s))
        rec = {str(d.id): d.recorded_at for d in s.scalars(select(Decision))}
    order = [p.get("id") for _, p in events if "id" in p]
    assert order.index(x) < order.index(y)
    kinds = {p["id"]: e for e, p in events if p.get("id") in (x, y)}
    assert kinds == {x: "decision.recorded", y: "decision.superseded"}
    for _, p in events:
        if p.get("id") in rec:
            assert p["at"] == rec[p["id"]].isoformat()  # original, not import time
    with hub.sessions() as s:
        again = list(enumerate_backfill(s))
    assert again == events  # deterministic


def test_governance_app_serves_the_backfill_open_verb(
    migrated_db, clean_db, stub_scope_client, monkeypatch
):
    """The wired surface: `create_app` hands the SDK router governance's
    enumerator, so the §7.1 open verb walks the store (not a 501)."""
    import anyio

    from snowline_governance.app import create_app
    from snowline_governance.db import session_scope

    monkeypatch.setenv("SNOWLINE_WEBHOOK_DISABLED", "1")
    app = create_app(
        scope_client=stub_scope_client(TREE),
        migrate_on_startup=False,
        register_on_startup=False,
    )
    with session_scope() as s:
        decisions.record_decision(s, "acme", stub_scope_client(TREE).resolve("acme")["id"], "d")

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 4242))
        async with httpx.AsyncClient(transport=transport, base_url="http://gov") as c:
            return await c.post("/replication-admin/backfill/open", json={
                "target_url": "http://roam/events/ingest", "secret": "s",
                "epoch": "bf-1", "source_id": "mini.governance",
            })

    resp = anyio.run(call)
    assert resp.status_code == 200, resp.text
    assert resp.json()["source_id"] == "mini.governance#backfill"
    assert resp.json()["enumerated"] == {"decision.recorded": 1}
