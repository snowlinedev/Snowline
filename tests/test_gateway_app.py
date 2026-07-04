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
from snowline_platform.gateway_app import build_surface_mounts, gateway_lifespan
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
    reg.upsert(PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"}))
    reg.upsert(PluginManifest(name="beta", base_url="http://beta", surfaces={"/mcp": "main"}))
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
    reg.upsert(PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"}))
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
    reg.upsert(
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


def test_surface_allowlist_over_http_core_is_governance_only(monkeypatch):
    """Issues #36+#38 end-to-end over the REAL streamable-HTTP transport, with
    REAL-shaped manifests (governance maps main+shadow, sidecar maps main — nothing
    maps `core` natively; fake `/core/mcp: core` manifests are the exact shape
    that masked #38): with `core` in BOTH envs (`SNOWLINE_SURFACES` mounts it,
    `SNOWLINE_SURFACE_PLUGINS` constrains it — the documented two-line contract,
    no auto-include), the mounted `/core/mcp` serves governance's PROJECTED main
    tools (not zero tools), `/mcp` serves BOTH plugins, and `/shadow/mcp` (no
    allowlist) still serves ONLY governance's native shadow tools — no
    projection leak."""
    monkeypatch.setenv("SNOWLINE_SURFACES", "main,shadow,core")
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", "core=governance")

    gov_main = make_stub_plugin("governance", ["record_decision", "get_decision"])
    gov_shadow = make_stub_plugin("governance", ["add_node", "get_decision"])
    sidecar = make_stub_plugin("sidecar", ["create_sidecar_item"])
    reg = PluginRegistry()
    reg.upsert(
        PluginManifest(
            name="governance",
            base_url="http://gov",
            surfaces={"/mcp": "main", "/shadow/mcp": "shadow"},
        )
    )
    reg.upsert(
        PluginManifest(
            name="sidecar", base_url="http://sidecar", surfaces={"/mcp": "main"}
        )
    )
    servers = {
        "http://gov/mcp": gov_main,
        "http://gov/shadow/mcp": gov_shadow,
        "http://sidecar/mcp": sidecar,
    }

    async def _list(session):
        return await session.list_tools()

    # Fresh app per surface (a session manager's run() is once-per-instance).
    # Both envs are read ONCE at create_app time (build_surface_mounts), so
    # `core` is mounted at /core/mcp with its frozen governance-only allowlist.
    main = {
        t.name
        for t in anyio.run(
            _run_against_surface,
            _app_with(reg, InMemoryConnector(servers)),
            "/mcp",
            _list,
        ).tools
    }
    core = {
        t.name
        for t in anyio.run(
            _run_against_surface,
            _app_with(reg, InMemoryConnector(servers)),
            "/core/mcp",
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

    # /mcp is the full composed daily driver — both plugins.
    assert "governance__record_decision" in main
    assert "sidecar__create_sidecar_item" in main
    # /core/mcp serves governance's PROJECTED main tools (issue #38: not empty)
    # — sidecar is physically absent.
    assert core == {"governance__record_decision", "governance__get_decision"}
    assert not any(name.startswith("sidecar__") for name in core)
    # /shadow/mcp has NO allowlist — pure manifest semantics, native shadow
    # tools only; governance's main tools must not leak here via projection.
    assert shadow == {"governance__add_node", "governance__get_decision"}
    assert "governance__record_decision" not in shadow


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("main", "malformed syntax (no '=')"),
        ("core=governance", "allowlist for a surface not in the mounted set"),
        ("*=governance", "'*' as a surface name ('*' is right-side only)"),
        ("main=Governance", "plugin token violating the manifest name rule"),
    ],
)
def test_create_app_fails_loud_at_boot_on_bad_allowlist(monkeypatch, raw, why):
    """Issue #36 review: a bad `SNOWLINE_SURFACE_PLUGINS` must kill boot —
    `create_app` itself raises `ConfigError` (parse + mounted-set cross-check
    happen once in `build_surface_mounts`, synchronously at app build), so a
    config mistake can never silently widen or dead-mount a surface. `why`
    exists to label each parametrized case."""
    from snowline_platform.config import ConfigError

    monkeypatch.setenv("SNOWLINE_SURFACES", "main,shadow")
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", raw)
    with pytest.raises(ConfigError):
        _app_with(PluginRegistry(), InMemoryConnector({}))


def test_non_mcp_routes_still_work():
    """Mounting the gateway doesn't break /health, /plugins, /scopes."""
    from starlette.testclient import TestClient

    reg = PluginRegistry()
    app = _app_with(reg, InMemoryConnector({}))
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/plugins").json() == {"plugins": []}


# --- Configurable surface set + prefix-specific mount ordering (#25) ----------


def test_config_surfaces_env_override_adds_surface_route(monkeypatch):
    """`SNOWLINE_SURFACES` drives the mounted surface set: an env-added surface
    gets its `/X/mcp` route, and the default is the documented (main, shadow)."""
    from snowline_platform import config

    monkeypatch.delenv("SNOWLINE_SURFACES", raising=False)
    assert config.surfaces() == ("main", "shadow")

    monkeypatch.setenv("SNOWLINE_SURFACES", "main,shadow,audit")
    assert config.surfaces() == ("main", "shadow", "audit")

    mounts = build_surface_mounts(PluginRegistry(), InMemoryConnector({}))
    routes = {m.route for m in mounts}
    assert routes == {"/mcp", "/shadow/mcp", "/audit/mcp"}


def test_config_surfaces_main_always_present(monkeypatch):
    """The root surface is always included even when the env omits it — it's the
    daily-driver root at `/mcp`. The list is also deduped, order-preserving."""
    from snowline_platform import config
    from snowline_platform.gateway_app import ROOT_SURFACE

    monkeypatch.setenv("SNOWLINE_SURFACES", "shadow,audit")
    result = config.surfaces()
    assert ROOT_SURFACE in result
    assert result == ("main", "shadow", "audit")

    monkeypatch.setenv("SNOWLINE_SURFACES", "main,main,shadow,shadow,audit")
    assert config.surfaces() == ("main", "shadow", "audit")


def test_root_surface_constant_honored(monkeypatch):
    """`surface_route` routes the ROOT_SURFACE to the bare `/mcp` and every other
    surface to `/X/mcp` — the root magic lives only in the constant."""
    from snowline_platform.gateway_app import ROOT_SURFACE, surface_route

    assert surface_route(ROOT_SURFACE) == "/mcp"
    assert surface_route("shadow") == "/shadow/mcp"
    assert surface_route("audit") == "/audit/mcp"


def test_mount_ordering_is_prefix_specific_not_len():
    """A route that is a path-PREFIX of another must be mounted AFTER it, so it
    can't shadow the deeper route under Starlette's first-match. This is the case
    `len(route)`-sort gets wrong: `/a/b/mcp` (len 8) must precede `/aa/mcp`
    (len 7) by length, but the real hazard is `/a/mcp` (a prefix of `/a/b/mcp`),
    which segment-depth ordering puts last where length ties would not."""
    from snowline_platform.gateway_app import mount_gateway

    class _RecordingApp:
        def __init__(self):
            self.mounted: list[str] = []

        def mount(self, route, app):
            self.mounted.append(route)

    # Surfaces "a" -> /a/mcp and "a/b" -> /a/b/mcp: /a/mcp is a string-prefix of
    # /a/b/mcp. The deeper (more path segments) route must mount first.
    mounts = build_surface_mounts(
        PluginRegistry(),
        InMemoryConnector({}),
        surfaces=("a", "a/b"),
    )
    app = _RecordingApp()
    mount_gateway(app, mounts)
    assert app.mounted.index("/a/b/mcp") < app.mounted.index("/a/mcp")

    # And the bare root surface (/mcp, fewest segments) is mounted last.
    mounts = build_surface_mounts(
        PluginRegistry(),
        InMemoryConnector({}),
        surfaces=("main", "shadow", "a/b"),
    )
    app = _RecordingApp()
    mount_gateway(app, mounts)
    assert app.mounted[-1] == "/mcp"
    assert app.mounted.index("/a/b/mcp") < app.mounted.index("/shadow/mcp")
