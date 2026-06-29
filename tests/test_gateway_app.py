"""End-to-end over the REAL streamable-HTTP transport: a real `ClientSession`
connects to the platform app's mounted `/mcp` (+ `/shadow/mcp`) and the gateway
aggregates + routes through the actual StreamableHTTP server + low-level Server.

The platform app is driven in-process via httpx's `ASGITransport` (no socket),
but the FULL MCP transport stack runs: the client opens a streamable-HTTP
session, `tools/list` and `tools/call` round-trip through the session manager and
the gateway's low-level Server, and the upstreams are stubbed only at the
connector seam (in-memory MCP plugins). This is what proves the meaty bit — the
streamable-HTTP proxy + session semantics — works end to end. Aggregation against
the REAL governance surface is `test_gateway_governance`."""

from __future__ import annotations

import json

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from snowline_platform.app import create_app
from snowline_platform.gateway_app import gateway_lifespan
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry
from snowline_platform.trust import Principal, TrustResolver

from ._gateway_helpers import InMemoryConnector, make_stub_plugin


class _AlwaysTrust:
    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _asgi_client(app) -> httpx.AsyncClient:
    """An httpx client routed at the in-process ASGI `app` (no socket) — handed
    to the mcp streamable-HTTP client so the FULL transport runs in-process."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://platform",
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    )


async def _run_against_surface(app, route, fn):
    """Enter the gateway lifespan, open a real ClientSession to `route` over the
    ASGI transport, and run `fn(session)`."""
    async with gateway_lifespan(app.state.gateway_mounts):
        async with _asgi_client(app) as http_client:
            async with streamable_http_client(
                f"http://platform{route}", http_client=http_client
            ) as (read, write, _sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await fn(session)


def _app_with(reg: PluginRegistry, connector: InMemoryConnector):
    return create_app(
        resolver=TrustResolver([_AlwaysTrust()]),
        registry=reg,
        migrate_on_startup=False,
        connector=connector,
    )


def test_two_plugins_merge_on_main_over_http():
    a = make_stub_plugin("alpha", ["read", "write"])
    b = make_stub_plugin("beta", ["ping"])
    reg = PluginRegistry()
    reg.register(PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"}))
    reg.register(PluginManifest(name="beta", base_url="http://beta", surfaces={"/mcp": "main"}))
    connector = InMemoryConnector({"http://alpha/mcp": a, "http://beta/mcp": b})
    app = _app_with(reg, connector)

    async def _list(session):
        return await session.list_tools()

    result = anyio.run(_run_against_surface, app, "/mcp", _list)
    names = {t.name for t in result.tools}
    assert names == {"alpha__read", "alpha__write", "beta__ping"}


def test_call_round_trips_over_http():
    a = make_stub_plugin("alpha", ["echo"])
    reg = PluginRegistry()
    reg.register(PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"}))
    connector = InMemoryConnector({"http://alpha/mcp": a})
    app = _app_with(reg, connector)

    async def _call(session):
        return await session.call_tool("alpha__echo", {"value": "round-trip"})

    res = anyio.run(_run_against_surface, app, "/mcp", _call)
    assert res.isError is not True
    payload = json.loads(res.content[0].text)
    assert payload == {"plugin": "alpha", "tool": "echo", "echo": "round-trip"}


def test_isolation_over_http_shadow_lacks_main_only_tool():
    gov_main = make_stub_plugin("governance", ["record_decision", "get_decision"])
    gov_shadow = make_stub_plugin("governance", ["add_node", "get_decision"])
    reg = PluginRegistry()
    reg.register(
        PluginManifest(
            name="governance",
            base_url="http://gov",
            surfaces={"/mcp": "main", "/shadow/mcp": "shadow"},
        )
    )
    servers = {"http://gov/mcp": gov_main, "http://gov/shadow/mcp": gov_shadow}

    async def _list(session):
        return await session.list_tools()

    # A fresh app per surface query: a StreamableHTTPSessionManager's run() may
    # only be entered once per instance, so each lifespan entry needs its own app.
    main = {
        t.name
        for t in anyio.run(
            _run_against_surface,
            _app_with(reg, InMemoryConnector(servers)),
            "/mcp",
            _list,
        ).tools
    }
    shadow = {
        t.name
        for t in anyio.run(
            _run_against_surface,
            _app_with(reg, InMemoryConnector(servers)),
            "/shadow/mcp",
            _list,
        ).tools
    }
    assert "governance__record_decision" in main
    assert "governance__record_decision" not in shadow
    assert "governance__add_node" in shadow


def test_non_mcp_routes_still_work():
    """Mounting the gateway doesn't break /health, /plugins, /scopes."""
    from starlette.testclient import TestClient

    reg = PluginRegistry()
    app = _app_with(reg, InMemoryConnector({}))
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/plugins").json() == {"plugins": []}
