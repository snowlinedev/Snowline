"""`snowline replicate pair` (replication-continuity §5/§10, issue #82): the
pairing library driven against in-process instances over the real SDK admin
surface. Covers discovery, the both-directions receiver-mints handshake,
`peer_source_id` wiring, one-sided-opt-in warnings, contract-version refusal,
vocabulary warnings, run-once idempotency, and an end-to-end convergence check
(emit → deliver → apply) over a stream the CLI just paired.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from snowline_platform import replication_pairing as pairing
from snowline_plugin_sdk.replication import emit as emit_mod
from snowline_plugin_sdk.replication.models import (
    ReplicationInboundStream,
    ReplicationSubscription,
)

from ._replication_helpers import (
    RoutedClient,
    make_participant,
    make_platform,
    plugin_entry,
)

GOV_EVENTS = ["decision.recorded", "decision.superseded"]
MEM_EVENTS = ["memory.set", "memory.forgotten"]


def _two_instances(*, spoke_plugins=("governance", "memory"),
                   gov_versions=(2, 2), gov_events=(GOV_EVENTS, GOV_EVENTS)):
    """A primary + a spoke, each a platform app plus governance/memory plugin
    apps, wired into one RoutedClient. `spoke_plugins` lets a plugin be dropped
    from the spoke (one-sided opt-in); `gov_versions`/`gov_events` skew
    governance's declared contract/vocabulary across the two sides."""
    apps = {
        "prim-platform": make_platform(plugins=[
            plugin_entry("governance", "http://prim-gov",
                         contract_version=gov_versions[0], events=list(gov_events[0])),
            plugin_entry("memory", "http://prim-mem", events=MEM_EVENTS),
        ]),
        "prim-gov": make_participant(),
        "prim-mem": make_participant(),
        "roam-platform": make_platform(plugins=[
            plugin_entry("governance", "http://roam-gov",
                         contract_version=gov_versions[1], events=list(gov_events[1])),
            *([plugin_entry("memory", "http://roam-mem", events=MEM_EVENTS)]
              if "memory" in spoke_plugins else []),
        ]),
        "roam-gov": make_participant(),
        "roam-mem": make_participant(),
    }
    client = RoutedClient({host: inst.app for host, inst in apps.items()})
    return apps, client


def _discover(client):
    local = pairing.discover_participants(client, "http://roam-platform", "roam")
    peer = pairing.discover_participants(client, "http://prim-platform", "primary")
    return local, peer


def test_discovery_reads_replication_blocks_and_adds_platform():
    _, client = _two_instances()
    local, _ = _discover(client)
    assert set(local) == {"governance", "memory", "platform"}
    gov = local["governance"]
    assert gov.source_id == "roam.governance"
    assert gov.admin_base == "http://roam-gov/replication-admin"
    assert gov.ingest_url == "http://roam-gov/events/ingest"
    # The platform participant is synthesized (not in /plugins) at §8's paths.
    plat = local["platform"]
    assert plat.source_id == "roam.platform"
    assert plat.ingest_url == "http://roam-platform/replication/events/ingest"
    assert plat.contract_version is None


def test_pair_opens_both_directions_with_peer_source_id_wired():
    apps, client = _two_instances()
    local, peer = _discover(client)
    plan = pairing.pair(client, local, peer, report=lambda _m: None)

    assert set(plan.to_pair) == {"governance", "memory", "platform"}
    assert not plan.refused and not plan.one_sided
    assert len(plan.streams) == 6  # 3 participants x 2 directions

    # The receiver holds an inbound registration for each direction; the sender
    # holds an outbound subscription whose peer_source_id names the REVERSE
    # stream (§3.2 causal context).
    with apps["roam-gov"].scope() as s:
        inbound = s.scalars(select(ReplicationInboundStream)).all()
        outbound = s.scalars(select(ReplicationSubscription)).all()
    assert [i.source_id for i in inbound] == ["primary.governance"]  # forward in
    assert len(outbound) == 1
    assert outbound[0].source_id == "roam.governance"
    assert outbound[0].peer_source_id == "primary.governance"

    with apps["prim-gov"].scope() as s:
        inbound = s.scalars(select(ReplicationInboundStream)).all()
        outbound = s.scalars(select(ReplicationSubscription)).all()
    assert [i.source_id for i in inbound] == ["roam.governance"]  # reverse in
    assert outbound[0].peer_source_id == "roam.governance"


def test_one_sided_opt_in_is_warned_and_skipped():
    _, client = _two_instances(spoke_plugins=("governance",))  # spoke has no memory
    local, peer = _discover(client)
    plan = pairing.pair(client, local, peer, report=lambda _m: None)
    assert "memory" in plan.one_sided
    assert "memory" not in plan.to_pair
    assert any("one-sided opt-in" in n and "memory" in n for n in plan.notes)


def test_contract_version_mismatch_refuses():
    _, client = _two_instances(gov_versions=(2, 3))
    local, peer = _discover(client)
    with pytest.raises(pairing.PairingError, match="contract_version mismatch"):
        pairing.pair(client, local, peer, report=lambda _m: None)
    # Nothing was paired — the refuse is raised before any handshake for governance.
    plan = pairing.plan_pairing(local, peer)
    assert plan.refused == ["governance"]


def test_vocabulary_mismatch_warns_but_pairs():
    _, client = _two_instances(
        gov_events=(GOV_EVENTS, GOV_EVENTS + ["decision.branched"])
    )
    local, peer = _discover(client)
    plan = pairing.pair(client, local, peer, report=lambda _m: None)
    assert "governance" in plan.vocab_warnings
    assert "governance" in plan.to_pair
    assert any("vocabulary differs" in n for n in plan.notes)


def test_pairing_runs_once_rerun_refuses():
    _, client = _two_instances()
    local, peer = _discover(client)
    pairing.pair(client, local, peer, report=lambda _m: None)
    with pytest.raises(pairing.PairingError, match="already paired|already exists"):
        pairing.pair(client, local, peer, report=lambda _m: None)


def test_paired_stream_converges_emit_deliver_apply(monkeypatch):
    """After pairing, a write emitted on primary.governance is delivered to and
    applied by roam.governance over the very stream the handshake opened —
    end-to-end proof the receiver holds the secret the sender signs with (§10
    both-directions verify)."""
    apps, client = _two_instances()
    local, peer = _discover(client)
    pairing.pair(client, local, peer, report=lambda _m: None)

    # Emit a decision on the primary's governance store (a subscription now
    # exists, so emit writes an outbox row with an emit-time seq).
    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "primary.governance")
    with apps["prim-gov"].scope() as s:
        envelopes = emit_mod.emit_event(
            s, "decision.recorded", {"id": "d1", "text": "hello"}
        )
    assert len(envelopes) == 1 and envelopes[0]["seq"] == 1

    # One delivery pass drains the primary's outbox into the spoke's ingest.
    with apps["prim-gov"].scope() as s:
        delivered = emit_mod.deliver_pending(s, client, reachability={})
    assert delivered == 1

    # The spoke applied exactly the emitted decision.
    assert [e["payload"]["id"] for e in apps["roam-gov"].applied] == ["d1"]
    # The spoke's inbound watermark advanced to 1.
    with apps["roam-gov"].scope() as s:
        stream = s.scalars(select(ReplicationInboundStream)).one()
    assert stream.gate_seq == 1 and stream.applied_seq == 1


def test_dry_run_plan_does_not_touch_the_wire():
    apps, client = _two_instances()
    local, peer = _discover(client)
    plan = pairing.plan_pairing(local, peer)
    assert set(plan.to_pair) == {"governance", "memory", "platform"}
    # No handshake ran: no inbound/outbound anywhere.
    with apps["roam-gov"].scope() as s:
        assert s.scalars(select(ReplicationInboundStream)).all() == []
        assert s.scalars(select(ReplicationSubscription)).all() == []


def test_mint_epoch_is_unique_and_sortable():
    epochs = [pairing.mint_epoch() for _ in range(50)]
    assert len(set(epochs)) == 50


def test_reachable_host_rewrites_peer_plugin_loopback_urls():
    """§4.1: a peer plugin advertises a LOOPBACK base_url to its own registry;
    discovering it over the tailnet rewrites the host onto the peer's tailnet
    host, PRESERVING the port (the serve posture maps port 1:1)."""
    apps = {
        "prim-platform": make_platform(plugins=[
            plugin_entry("governance", "http://127.0.0.1:8801",
                         events=["decision.recorded"]),
        ]),
    }
    client = RoutedClient({h: inst.app for h, inst in apps.items()})
    peer = pairing.discover_participants(
        client, "http://prim-platform", "primary", reachable_host="primary.tailnet"
    )
    assert peer["governance"].admin_base == "http://primary.tailnet:8801/replication-admin"
    assert peer["governance"].ingest_url == "http://primary.tailnet:8801/events/ingest"
    # The platform participant is addressed via platform_url, never rewritten.
    assert peer["platform"].admin_base == "http://prim-platform/replication-admin"


def test_rehost_preserves_port_and_path():
    assert pairing._rehost("http://127.0.0.1:8801/x", "h.tailnet") == "http://h.tailnet:8801/x"
    assert pairing._rehost("http://127.0.0.1:8801", "h.tailnet") == "http://h.tailnet:8801"
    assert pairing._rehost("http://127.0.0.1:8801", None) == "http://127.0.0.1:8801"
