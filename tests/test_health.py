"""The health poller — status mapping, concurrent rounds, and the gateway
route-around it drives (health.md).

All deterministic: an `httpx.MockTransport` stands in for the plugins' health
endpoints (2xx / non-2xx / transport error), so no sockets and no timing
dependence in the status tests. The loop test uses a tiny interval + a bounded
wait, then cancels."""

from __future__ import annotations

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
        reg.register(m)
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
