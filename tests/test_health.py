"""The health poller — status mapping, concurrent rounds, and the gateway
route-around it drives (health.md).

All deterministic: an `httpx.MockTransport` stands in for the plugins' health
endpoints (2xx / non-2xx / transport error), so no sockets and no timing
dependence in the status tests. The loop test uses a tiny interval + a bounded
wait, then cancels."""

from __future__ import annotations

from datetime import datetime

import anyio
import httpx

from snowline_platform import health
from snowline_platform.gateway import discover_upstreams
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry, PluginStatus


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _reg(*manifests: PluginManifest) -> PluginRegistry:
    reg = PluginRegistry()
    for m in manifests:
        reg.upsert(m)
    return reg


def test_health_url_composes_base_and_path():
    m = PluginManifest(name="gov", base_url="http://gov:1", health_path="/healthz")
    assert health.health_url(m) == "http://gov:1/healthz"
    # base_url trailing slash is trimmed by the manifest validator.
    m2 = PluginManifest(name="gov", base_url="http://gov:1/")
    assert health.health_url(m2) == "http://gov:1/health"


def test_check_up_on_2xx():
    reg = _reg(PluginManifest(name="gov", base_url="http://gov:1"))
    entry = reg.get("gov")

    async def go():
        async with _mock_client(lambda req: httpx.Response(200)) as c:
            return await health.check(c, entry)

    assert anyio.run(go) is PluginStatus.UP


def test_check_down_on_non_2xx():
    reg = _reg(PluginManifest(name="gov", base_url="http://gov:1"))
    entry = reg.get("gov")

    async def go():
        async with _mock_client(lambda req: httpx.Response(503)) as c:
            return await health.check(c, entry)

    assert anyio.run(go) is PluginStatus.DOWN


def test_check_down_on_transport_error():
    """A crashed-local (connection refused) / unreachable-remote (DNS/TLS/timeout)
    error is caught and mapped to DOWN, never raised."""
    reg = _reg(PluginManifest(name="gov", base_url="http://gov:1"))
    entry = reg.get("gov")

    def boom(req):
        raise httpx.ConnectError("connection refused", request=req)

    async def go():
        async with _mock_client(boom) as c:
            return await health.check(c, entry)

    assert anyio.run(go) is PluginStatus.DOWN


def test_poll_once_updates_registry_for_mixed_health():
    reg = _reg(
        PluginManifest(name="up-plugin", base_url="http://up:1"),
        PluginManifest(name="down-plugin", base_url="http://down:1"),
    )

    def handler(req: httpx.Request) -> httpx.Response:
        # host 'up' is healthy, 'down' returns 500
        return httpx.Response(200 if req.url.host == "up" else 500)

    async def go():
        async with _mock_client(handler) as c:
            return await health.poll_once(reg, c)

    results = anyio.run(go)
    assert results == {
        "up-plugin": PluginStatus.UP,
        "down-plugin": PluginStatus.DOWN,
    }
    assert reg.get("up-plugin").status is PluginStatus.UP
    assert reg.get("down-plugin").status is PluginStatus.DOWN


def test_poll_once_empty_registry_is_noop():
    async def go():
        async with _mock_client(lambda req: httpx.Response(200)) as c:
            return await health.poll_once(PluginRegistry(), c)

    assert anyio.run(go) == {}


def test_poll_drives_gateway_route_around():
    """The end-to-end point of #3: a DOWN plugin disappears from a surface; a
    healthy one stays. The gateway code is unchanged — only the status the poller
    sets differs."""
    reg = _reg(
        PluginManifest(name="alive", base_url="http://alive:1", surfaces={"/mcp": "main"}),
        PluginManifest(name="dead", base_url="http://dead:1", surfaces={"/mcp": "main"}),
    )
    # Before any poll both are UNKNOWN → both routable.
    assert {u.plugin_name for u in discover_upstreams(reg, "main")} == {
        "alive",
        "dead",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if req.url.host == "alive" else 502)

    async def go():
        async with _mock_client(handler) as c:
            await health.poll_once(reg, c)

    anyio.run(go)
    # After the poll the dead plugin is routed around; the live one remains.
    assert {u.plugin_name for u in discover_upstreams(reg, "main")} == {"alive"}
    assert reg.get("dead").status is PluginStatus.DOWN


def test_poll_recovers_a_plugin_back_into_the_surface():
    """A DOWN plugin that starts returning 2xx flips back to UP next round AND
    reappears in the gateway's discovered upstreams (the round-trip, not just the
    status field)."""
    reg = _reg(
        PluginManifest(name="gov", base_url="http://gov:1", surfaces={"/mcp": "main"})
    )
    state = {"healthy": False}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if state["healthy"] else 500)

    async def go():
        async with _mock_client(handler) as c:
            await health.poll_once(reg, c)
            first = reg.get("gov").status
            first_routable = {u.plugin_name for u in discover_upstreams(reg, "main")}
            state["healthy"] = True
            await health.poll_once(reg, c)
            second_routable = {u.plugin_name for u in discover_upstreams(reg, "main")}
            return first, first_routable, reg.get("gov").status, second_routable

    first, first_routable, second, second_routable = anyio.run(go)
    assert first is PluginStatus.DOWN
    assert first_routable == set()  # routed around while DOWN
    assert second is PluginStatus.UP
    assert second_routable == {"gov"}  # back in the surface on recovery


def test_poll_once_does_not_resurrect_a_plugin_unregistered_mid_round():
    """If a plugin is unregistered DURING a round, set_status is a no-op — the
    poller never re-adds it (health.md concurrency safety)."""
    reg = _reg(PluginManifest(name="gov", base_url="http://gov:1"))

    def handler(req: httpx.Request) -> httpx.Response:
        # Remove the plugin while its health GET is in flight.
        reg.unregister("gov")
        return httpx.Response(200)

    async def go():
        async with _mock_client(handler) as c:
            return await health.poll_once(reg, c)

    anyio.run(go)  # must not raise
    assert [e.manifest.name for e in reg.list()] == []  # not resurrected


def test_config_health_getters_defaults_and_env(monkeypatch):
    from snowline_platform import config

    monkeypatch.delenv("SNOWLINE_HEALTH_POLL_INTERVAL", raising=False)
    monkeypatch.delenv("SNOWLINE_HEALTH_POLL_TIMEOUT", raising=False)
    assert config.health_poll_interval() == 15.0
    assert config.health_poll_timeout() == 5.0

    monkeypatch.setenv("SNOWLINE_HEALTH_POLL_INTERVAL", "3")
    monkeypatch.setenv("SNOWLINE_HEALTH_POLL_TIMEOUT", "0.5")
    assert config.health_poll_interval() == 3.0
    assert config.health_poll_timeout() == 0.5


def test_app_lifespan_starts_poller_and_marks_unreachable_down():
    """Wiring test: building the app with poll_health=True and entering its
    LIFESPAN actually starts the poller (correct partial args, flag, config
    getters) and shuts it down cleanly. An unreachable plugin (port 1, refused)
    gets marked DOWN by the real loop, then the lifespan exit cancels it without
    hanging."""
    from snowline_platform.app import create_app
    from snowline_platform.trust import Principal, TrustResolver

    class _AlwaysTrust:
        def resolve(self, peer_ip, headers):
            return Principal(id="t", source="test")

    reg = _reg(PluginManifest(name="gone", base_url="http://127.0.0.1:1"))
    app = create_app(
        resolver=TrustResolver([_AlwaysTrust()]),
        registry=reg,
        migrate_on_startup=False,
        poll_health=True,
    )

    async def go():
        async with app.router.lifespan_context(app):
            # First poll fires immediately; 127.0.0.1:1 refuses fast -> DOWN.
            with anyio.move_on_after(5.0):
                while reg.get("gone").status is PluginStatus.UNKNOWN:
                    await anyio.sleep(0.02)
            return reg.get("gone").status
        # context exit cancels the poller — if that hung, this test would too.

    assert anyio.run(go) is PluginStatus.DOWN


def test_health_loop_warns_when_no_external_plugins(caplog):
    """The hollow-gateway detector (issue #39): no EXTERNAL plugin on the second
    poll round — one boot round of grace for the plugins' registration
    heartbeats — warns loudly, exactly once per episode. The always-present
    platform self-entry (decision 0503fff0) is seeded here to prove it does NOT
    silence the signal (the #148 review follow-up)."""
    import logging

    from snowline_platform.platform_tools import platform_self_manifest

    reg = PluginRegistry()
    reg.upsert(platform_self_manifest())  # healthy self-entry, still hollow

    async def go():
        async with _mock_client(lambda req: httpx.Response(200)) as c:
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: health.health_poll_loop(
                        reg, interval=0.01, timeout=1.0, client=c
                    )
                )
                with anyio.move_on_after(2.0):
                    while not any(
                        "no external plugins" in r.message for r in caplog.records
                    ):
                        await anyio.sleep(0.005)
                tg.cancel_scope.cancel()

    with caplog.at_level(logging.WARNING, logger="snowline_platform.health"):
        anyio.run(go)
    warnings = [r for r in caplog.records if "no external plugins" in r.message]
    assert len(warnings) == 1  # loud once, then quiet — not a line per round


def test_health_loop_polls_then_cancels():
    """The background loop polls at its interval and unwinds cleanly on cancel."""
    reg = _reg(PluginManifest(name="gov", base_url="http://gov:1"))

    async def go():
        async with _mock_client(lambda req: httpx.Response(200)) as c:
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: health.health_poll_loop(
                        reg, interval=0.01, timeout=1.0, client=c
                    )
                )
                # Wait (bounded) for the first round to mark the plugin.
                with anyio.move_on_after(2.0):
                    while reg.get("gov").status is PluginStatus.UNKNOWN:
                        await anyio.sleep(0.005)
                tg.cancel_scope.cancel()
        return reg.get("gov").status

    assert anyio.run(go) is PluginStatus.UP


# --- degraded (issue #241) -----------------------------------------------------

_DEGRADED_BODY = {
    "status": "degraded",
    "plugin": "pm",
    "degraded_reason": "peer mbp.pm not delivering for 1200s (> 900s): ConnectError: refused",
    "replication": {"status": "degraded", "degraded_reason": "x", "outbox": {}},
}


def test_check_degraded_on_self_reported_degraded_body():
    reg = _reg(PluginManifest(name="pm", base_url="http://pm:1", surfaces={"/mcp": "main"}))

    async def go():
        async with _mock_client(lambda req: httpx.Response(200, json=_DEGRADED_BODY)) as c:
            await health.poll_once(reg, c)

    anyio.run(go)
    entry = reg.get("pm")
    assert entry.status is PluginStatus.DEGRADED
    assert entry.degraded_reason == _DEGRADED_BODY["degraded_reason"]
    # Degraded is still routable — only DOWN is routed around.
    assert {u.plugin_name for u in discover_upstreams(reg, "main")} == {"pm"}


def test_degraded_reason_falls_back_to_replication_block_then_generic():
    reg = _reg(PluginManifest(name="pm", base_url="http://pm:1"))
    entry = reg.get("pm")

    async def go(body):
        async with _mock_client(lambda req: httpx.Response(200, json=body)) as c:
            return await health.probe(c, entry)

    assert anyio.run(go, {"status": "degraded", "replication": {"degraded_reason": "r"}}) == (
        PluginStatus.DEGRADED, "r"
    )
    assert anyio.run(go, {"status": "degraded"}) == (
        PluginStatus.DEGRADED, "plugin reported degraded"
    )
    # ok / non-dict bodies stay UP (2xx is still the contract).
    assert anyio.run(go, {"status": "ok"}) == (PluginStatus.UP, None)
    assert anyio.run(go, ["x"]) == (PluginStatus.UP, None)


def test_recovery_clears_degraded_reason():
    reg = _reg(PluginManifest(name="pm", base_url="http://pm:1"))
    state = {"body": _DEGRADED_BODY}

    async def go():
        async with _mock_client(lambda req: httpx.Response(200, json=state["body"])) as c:
            await health.poll_once(reg, c)
            state["body"] = {"status": "ok"}
            await health.poll_once(reg, c)

    anyio.run(go)
    assert reg.get("pm").status is PluginStatus.UP
    assert reg.get("pm").degraded_reason is None


def test_non_2xx_degraded_body_is_still_down():
    """DOWN semantics unchanged: a non-2xx is DOWN whatever the body says."""
    reg = _reg(PluginManifest(name="pm", base_url="http://pm:1"))

    async def go():
        async with _mock_client(lambda req: httpx.Response(503, json=_DEGRADED_BODY)) as c:
            return await health.poll_once(reg, c)

    assert anyio.run(go) == {"pm": PluginStatus.DOWN}
    assert reg.get("pm").degraded_reason is None


def test_platform_self_entry_judged_by_its_replication_block_only():
    """The platform's top-level status is the AGGREGATE; reading it back onto
    the self-entry would mark the platform degraded for another plugin."""
    from snowline_platform.platform_tools import PLATFORM_PLUGIN_NAME

    reg = _reg(PluginManifest(name=PLATFORM_PLUGIN_NAME, base_url="http://plat:1"))
    entry = reg.get(PLATFORM_PLUGIN_NAME)

    async def go(body):
        async with _mock_client(lambda req: httpx.Response(200, json=body)) as c:
            return await health.probe(c, entry)

    aggregate_only = {
        "status": "degraded",
        "degraded_reason": "plugin pm: x",
        "replication": {"status": "ok", "degraded_reason": None},
    }
    assert anyio.run(go, aggregate_only) == (PluginStatus.UP, None)
    own = {
        "status": "degraded",
        "replication": {"status": "degraded", "degraded_reason": "tick raised"},
    }
    assert anyio.run(go, own) == (PluginStatus.DEGRADED, "tick raised")


def _app_client():
    from starlette.testclient import TestClient

    from snowline_platform.app import create_app
    from snowline_platform.trust import Principal, TrustResolver

    class _AlwaysTrust:
        def resolve(self, peer_ip, headers):
            return Principal(id="test-owner", source="test")

    app = create_app(resolver=TrustResolver([_AlwaysTrust()]), migrate_on_startup=False)
    return app, TestClient(app)


def test_platform_health_aggregate_ok_and_down_unchanged():
    app, client = _app_client()
    reg = app.state.registry
    reg.upsert(PluginManifest(name="gov", base_url="http://gov:1"))
    reg.upsert(PluginManifest(name="mem", base_url="http://mem:1"))
    reg.set_status("gov", PluginStatus.UP)
    reg.set_status("mem", PluginStatus.DOWN)

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    # A DOWN plugin is shown but does not change the overall status.
    assert body["status"] == "ok"
    assert body["plugins"] == {"gov": {"status": "up"}, "mem": {"status": "down"}}
    assert "degraded_reason" not in body
    assert body["replication"]["status"] == "ok"
    assert set(body["replication"]["outbox"]) >= {
        "pending", "oldest_pending_age_s", "peers", "tick"
    }


def test_platform_health_aggregate_degraded_names_the_plugin():
    app, client = _app_client()
    reg = app.state.registry
    reg.upsert(PluginManifest(name="pm", base_url="http://pm:1"))
    reg.upsert(PluginManifest(name="gov", base_url="http://gov:1"))
    reg.set_status("pm", PluginStatus.DEGRADED, degraded_reason="peer mbp.pm not delivering")
    reg.set_status("gov", PluginStatus.UP)

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["plugins"]["pm"] == {
        "status": "degraded",
        "replication_degraded_reason": "peer mbp.pm not delivering",
    }
    assert body["plugins"]["gov"] == {"status": "up"}
    assert body["degraded_reason"] == "plugin pm: peer mbp.pm not delivering"
    # /plugins carries the reason for the dashboard.
    listed = {p["name"]: p for p in client.get("/plugins").json()["plugins"]}
    assert listed["pm"]["status"] == "degraded"
    assert listed["pm"]["degraded_reason"] == "peer mbp.pm not delivering"


def test_platform_health_degrades_on_its_own_replication(monkeypatch):
    """The platform's OWN delivery loop counts too (it replicates scopes)."""
    from snowline_plugin_sdk.replication.health import DELIVERY_HEALTH

    app, client = _app_client()
    for _ in range(3):
        DELIVERY_HEALTH.record_tick(datetime(2026, 7, 4), OverflowError("boom"))
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["degraded_reason"] == (
        "delivery tick raised 3 consecutive times: OverflowError: boom"
    )
    assert body["replication"]["status"] == "degraded"


def test_platform_health_outbox_age_on_postgres_is_zone_correct(clean_db, monkeypatch):
    """Against real Postgres: `created_at` is a naive `now()` default in the
    SESSION timezone (not UTC), so the age must use the DB's own clock — a
    just-emitted row is seconds old, not hours, on a non-UTC server (the hub
    runs America/Detroit)."""
    from snowline_plugin_sdk.replication import emit

    from snowline_platform.db import session_scope

    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "test.platform")
    with session_scope() as s:
        emit.create_outbound_subscription(
            s, "http://peer:1/events/ingest", "x", ["scope.created"],
            epoch="e1", peer_source_id="spoke.platform",
        )
        emit.emit_event(s, "scope.created", {"slug": "a"})

    _, client = _app_client()
    outbox = client.get("/health").json()["replication"]["outbox"]
    assert outbox["pending"] == 1
    assert outbox["peers"]["spoke.platform"]["pending"] == 1
    assert 0 <= outbox["oldest_pending_age_s"] < 600
