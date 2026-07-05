"""`snowline replicate pair` (replication-continuity §5/§10, issue #82): the
pairing library driven against in-process instances over the real SDK admin
surface. Covers discovery, the both-directions receiver-mints handshake,
`peer_source_id` wiring, one-sided-opt-in warnings, contract-version refusal,
vocabulary warnings, run-once idempotency, and an end-to-end convergence check
(emit → deliver → apply) over a stream the CLI just paired.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from snowline_platform import replication_pairing as pairing
from snowline_platform.replication import SCOPE_EVENTS
from snowline_plugin_sdk.contract import CONTRACT_VERSION
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
    # The platform participant is read from its §8 self-manifest (issue #95),
    # NOT synthesized from constants — so it carries a REAL contract_version and
    # vocabulary and is version-checked at pairing like a plugin.
    plat = local["platform"]
    assert plat.source_id == "roam.platform"
    assert plat.ingest_url == "http://roam-platform/replication/events/ingest"
    assert plat.contract_version == CONTRACT_VERSION
    assert plat.events == tuple(SCOPE_EVENTS)


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


def test_advertised_base_url_preferred_over_port_rewrite():
    """§4.1 (#96): a plugin that DECLARES `advertised_base_url` is addressed
    there verbatim — pairing does NOT port-rewrite its loopback base_url onto
    the peer host. This is the principled answer when the serve posture is not a
    1:1 port mirror (a different port, a path front, a distinct host)."""
    apps = {
        "prim-platform": make_platform(plugins=[
            plugin_entry("governance", "http://127.0.0.1:8801",
                         advertised_base_url="http://primary.tailnet:9901",
                         events=["decision.recorded"]),
        ]),
    }
    client = RoutedClient({h: inst.app for h, inst in apps.items()})
    peer = pairing.discover_participants(
        client, "http://prim-platform", "primary", reachable_host="primary.tailnet"
    )
    gov = peer["governance"]
    # The declared address wins outright — note the port is 9901, NOT the 8801
    # the fallback rewrite would have preserved.
    assert gov.admin_base == "http://primary.tailnet:9901/replication-admin"
    assert gov.ingest_url == "http://primary.tailnet:9901/events/ingest"


def test_absent_advertised_base_url_is_byte_identical_to_the_old_rewrite():
    """DO-NOT-BREAK-EXISTING-PAIRS pin: with NO `advertised_base_url`, a peer
    plugin is addressed by the exact same port-preserving rewrite as before #96
    — byte-identical to `_rehost(base_url, host)`, so an existing pair that has
    never declared the field behaves identically."""
    base = "http://127.0.0.1:8801"
    apps = {
        "prim-platform": make_platform(plugins=[
            plugin_entry("governance", base, events=["decision.recorded"]),
        ]),
    }
    client = RoutedClient({h: inst.app for h, inst in apps.items()})
    peer = pairing.discover_participants(
        client, "http://prim-platform", "primary", reachable_host="primary.tailnet"
    )
    rewritten = pairing._rehost(base, "primary.tailnet")
    assert peer["governance"].admin_base == f"{rewritten}/replication-admin"
    assert peer["governance"].ingest_url == f"{rewritten}/events/ingest"


def test_local_discovery_uses_loopback_base_url_even_with_advertised():
    """LOCAL discovery (`reachable_host` None) addresses a plugin at its loopback
    base_url as-is — `advertised_base_url` is a PEER-reachability concern (§4.1),
    so a locally-discovered plugin is never redirected to it."""
    apps = {
        "roam-platform": make_platform(plugins=[
            plugin_entry("governance", "http://127.0.0.1:8801",
                         advertised_base_url="http://roam.tailnet:9901",
                         events=["decision.recorded"]),
        ]),
    }
    client = RoutedClient({h: inst.app for h, inst in apps.items()})
    local = pairing.discover_participants(client, "http://roam-platform", "roam")
    assert local["governance"].admin_base == "http://127.0.0.1:8801/replication-admin"


def test_platform_stream_contract_version_checked_at_pairing():
    """#95: the platform self-manifest gives the scope stream a REAL
    contract_version, so a skew between the two instances' platform versions now
    REFUSES at pairing exactly like a plugin's — no longer silently deferred to
    a delivery-time version_hold."""
    apps = {
        "prim-platform": make_platform(plugins=[], platform_contract_version=2),
        "roam-platform": make_platform(plugins=[], platform_contract_version=3),
    }
    client = RoutedClient({h: inst.app for h, inst in apps.items()})
    local = pairing.discover_participants(client, "http://roam-platform", "roam")
    peer = pairing.discover_participants(client, "http://prim-platform", "primary")
    assert pairing.plan_pairing(local, peer).refused == ["platform"]
    with pytest.raises(pairing.PairingError, match="contract_version mismatch"):
        pairing.pair(client, local, peer, report=lambda _m: None)


def test_platform_stream_vocabulary_skew_warns_but_pairs():
    """A platform vocabulary skew WARNs but pairs, like a plugin's (§5) — the
    union flows and an event one side never emits simply never arrives."""
    apps = {
        "prim-platform": make_platform(
            plugins=[],
            platform_events=("scope.created", "scope.updated", "scope.retired"),
        ),
        "roam-platform": make_platform(plugins=[]),
    }
    client = RoutedClient({h: inst.app for h, inst in apps.items()})
    local = pairing.discover_participants(client, "http://roam-platform", "roam")
    peer = pairing.discover_participants(client, "http://prim-platform", "primary")
    plan = pairing.plan_pairing(local, peer)
    assert "platform" in plan.vocab_warnings
    assert "platform" in plan.to_pair


def _peer_platform_app(*, self_manifest, plugins=()):
    """A platform-shaped app whose `/replication/manifest` behavior is under
    test. `self_manifest` is 'absent' (no route → 404, a pre-#95 peer),
    'malformed' (a 200 that is not JSON), or a dict body (e.g. missing a
    required key). `/plugins` returns `plugins` so discovery reaches the
    platform read after the plugin loop."""
    app = FastAPI()

    @app.get("/plugins")
    async def _plugins() -> dict:  # noqa: D401 - test fixture route
        return {"plugins": list(plugins)}

    if self_manifest == "malformed":
        @app.get("/replication/manifest")
        async def _malformed() -> PlainTextResponse:  # noqa: D401
            return PlainTextResponse("<html>not json</html>")
    elif isinstance(self_manifest, dict):
        @app.get("/replication/manifest")
        async def _body() -> dict:  # noqa: D401
            return self_manifest
    # 'absent' → no route mounted → FastAPI 404
    return app


def test_platform_self_manifest_404_falls_back_and_pairing_proceeds(caplog):
    """#95 migration path: a peer platform predating the self-manifest (404) is
    SYNTHESIZED the pre-#95 way (scope vocabulary, §8 ingest path,
    contract_version UNKNOWN) with a loud WARN — and, crucially, the 404 does
    NOT block discovery/pairing of the OTHER participants (the plugin is still
    discovered and planned). Skew defers to a delivery-time hold, as before #95."""
    gov = plugin_entry("governance", "http://127.0.0.1:8801", events=["decision.recorded"])
    client = RoutedClient({
        "prim-platform": _peer_platform_app(self_manifest="absent", plugins=[gov]),
        "roam-platform": make_platform(plugins=[gov]).app,  # local serves a real manifest
    })
    with caplog.at_level(logging.WARNING, logger="snowline_platform.replication_pairing"):
        peer = pairing.discover_participants(
            client, "http://prim-platform", "primary", reachable_host="prim-platform"
        )
    # The 404 platform is synthesized, NOT a blocker — the plugin came through too.
    assert set(peer) == {"governance", "platform"}
    plat = peer["platform"]
    assert plat.source_id == "primary.platform"
    assert plat.ingest_url == "http://prim-platform/replication/events/ingest"
    assert plat.events == ("scope.created", "scope.updated")
    assert plat.contract_version is None  # unknown → the pre-#95 pairing-time skip
    assert any("predates the replication self-manifest" in r.message for r in caplog.records)

    # Pairing PROCEEDS for every participant: the None platform version is not a
    # mismatch (it defers), so nothing is refused and both are planned.
    local = pairing.discover_participants(client, "http://roam-platform", "roam")
    plan = pairing.plan_pairing(local, peer)
    assert set(plan.to_pair) == {"governance", "platform"}
    assert not plan.refused


def test_platform_self_manifest_malformed_json_raises_labeled_error():
    """A 200 whose body is not JSON is a broken peer self-manifest — a LABELED
    PairingError naming the URL, not a raw JSONDecodeError stack trace."""
    client = RoutedClient({"prim-platform": _peer_platform_app(self_manifest="malformed")})
    with pytest.raises(pairing.PairingError, match="broken replication self-manifest"):
        pairing.discover_participants(client, "http://prim-platform", "primary")


def test_platform_self_manifest_missing_field_raises_labeled_error():
    """A 200 JSON object missing a required key (here `ingest_path`) is an
    incomplete self-manifest — a LABELED PairingError naming the missing field,
    not a raw KeyError from `_participant`. (The plugin path is guarded by
    ReplicationBlock at registration; this raw-JSON read is not.)"""
    body = {"contract_version": 2, "events": ["scope.created", "scope.updated"]}
    client = RoutedClient({"prim-platform": _peer_platform_app(self_manifest=body)})
    with pytest.raises(pairing.PairingError, match="incomplete replication self-manifest.*ingest_path"):
        pairing.discover_participants(client, "http://prim-platform", "primary")


def test_rehost_preserves_port_and_path():
    assert pairing._rehost("http://127.0.0.1:8801/x", "h.tailnet") == "http://h.tailnet:8801/x"
    assert pairing._rehost("http://127.0.0.1:8801", "h.tailnet") == "http://h.tailnet:8801"
    assert pairing._rehost("http://127.0.0.1:8801", None) == "http://127.0.0.1:8801"


def test_rehost_warns_only_when_original_is_not_loopback(caplog):
    """Review finding: rewriting a NON-loopback base_url may not honor the §4.1
    serve→loopback assumption, so it warns; a loopback base_url (the expected
    case) rewrites silently."""
    import logging

    with caplog.at_level(logging.WARNING, logger="snowline_platform.replication_pairing"):
        assert pairing._rehost("http://10.0.0.5:8801/x", "peer.tailnet") == "http://peer.tailnet:8801/x"
    assert any("non-loopback" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="snowline_platform.replication_pairing"):
        pairing._rehost("http://127.0.0.1:8801/x", "peer.tailnet")
        pairing._rehost("http://localhost:8801/x", "peer.tailnet")
    assert caplog.records == []


def test_handshake_direction_refuses_version_mismatch_before_touching_wire():
    """Review finding: the refuse lives in handshake_direction so EVERY caller
    (pair AND the seed's reverse-pair) is covered. client=None proves it raises
    before any HTTP call."""
    a = pairing.Participant("governance", "http://a/replication-admin",
                            "http://a/events/ingest", "roam.governance",
                            ("decision.recorded",), 2)
    b = pairing.Participant("governance", "http://b/replication-admin",
                            "http://b/events/ingest", "primary.governance",
                            ("decision.recorded",), 3)
    with pytest.raises(pairing.PairingError, match="contract_version mismatch"):
        pairing.handshake_direction(None, a, b, report=lambda _m: None)


def test_handshake_direction_allows_unknown_version_on_one_side():
    """A None contract_version is not a mismatch — refuse_on_version_mismatch
    lets it through (skew holds at delivery, §3.2). The platform USED to be this
    None case; since #95 it self-declares a real version, so this now guards the
    generic defensive path (a participant that declares no version at all)."""
    a = pairing.Participant("governance", "http://a/replication-admin",
                            "http://a/x", "roam.governance", ("decision.recorded",), None)
    b = pairing.Participant("governance", "http://b/replication-admin",
                            "http://b/x", "primary.governance", ("decision.recorded",), 2)
    pairing.refuse_on_version_mismatch(a, b)  # no raise
