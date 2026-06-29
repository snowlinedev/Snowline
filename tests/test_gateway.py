"""The gateway's composition logic — aggregation, namespacing/collision, routing,
health-aware route-around, and isolation-by-composition (gateway.md §2/§4/§6).

These drive `SurfaceGateway` directly with an in-memory connector + tiny FastMCP
stub plugins, so they prove the COMPOSITION semantics without any HTTP or DB. The
streamable-HTTP transport wiring is covered separately by `test_gateway_app`; the
real governance upstream end-to-end by `test_gateway_governance`."""

from __future__ import annotations

import anyio

from snowline_platform.gateway import (
    SurfaceGateway,
    discover_upstreams,
    namespaced_name,
    split_namespaced,
)
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry, PluginStatus

from ._gateway_helpers import InMemoryConnector, make_stub_plugin


def _registry(*manifests: PluginManifest) -> PluginRegistry:
    reg = PluginRegistry()
    for m in manifests:
        reg.register(m)
    return reg


def test_namespacing_roundtrip():
    assert namespaced_name("governance", "record_decision") == (
        "governance__record_decision"
    )
    assert split_namespaced("governance__record_decision") == (
        "governance",
        "record_decision",
    )


def test_split_rejects_unnamespaced():
    import pytest

    with pytest.raises(ValueError):
        split_namespaced("record_decision")


def test_discover_filters_by_surface_and_skips_down():
    gov = PluginManifest(
        name="governance",
        base_url="http://gov:1",
        surfaces={"/mcp": "main", "/shadow/mcp": "shadow"},
    )
    pm = PluginManifest(
        name="pm", base_url="http://pm:1", surfaces={"/mcp": "main"}
    )
    reg = _registry(gov, pm)

    main = discover_upstreams(reg, "main")
    assert {(u.plugin_name, u.plugin_path) for u in main} == {
        ("governance", "/mcp"),
        ("pm", "/mcp"),
    }
    shadow = discover_upstreams(reg, "shadow")
    assert {(u.plugin_name, u.plugin_path) for u in shadow} == {
        ("governance", "/shadow/mcp")
    }

    # Mark pm down -> route-around: it disappears from `main`.
    reg.set_status("pm", PluginStatus.DOWN)
    main = discover_upstreams(reg, "main")
    assert {u.plugin_name for u in main} == {"governance"}


def test_default_surface_is_main_when_unmapped():
    # A manifest with no `surfaces` defaults to {mcp_path: 'main'}.
    pm = PluginManifest(name="pm", base_url="http://pm:1")
    reg = _registry(pm)
    assert {u.plugin_name for u in discover_upstreams(reg, "main")} == {"pm"}
    assert discover_upstreams(reg, "shadow") == []


def test_aggregates_two_plugins_on_main():
    """Two plugins mapped to `main` -> their tools merge into one surface, each
    namespaced by plugin (collision policy)."""
    a = make_stub_plugin("alpha", ["read", "write"])
    b = make_stub_plugin("beta", ["read", "ping"])  # 'read' collides
    reg = _registry(
        PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"}),
        PluginManifest(name="beta", base_url="http://beta", surfaces={"/mcp": "main"}),
    )
    connector = InMemoryConnector(
        {"http://alpha/mcp": a, "http://beta/mcp": b}
    )
    gw = SurfaceGateway(reg, "main", connector)

    tools = anyio.run(gw.list_tools)
    names = {t.name for t in tools}
    # Collision policy = namespace-by-plugin: both 'read's survive, distinct.
    assert names == {
        "alpha__read",
        "alpha__write",
        "beta__read",
        "beta__ping",
    }


def test_call_routes_to_owning_plugin():
    a = make_stub_plugin("alpha", ["echo"])
    b = make_stub_plugin("beta", ["echo"])
    reg = _registry(
        PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"}),
        PluginManifest(name="beta", base_url="http://beta", surfaces={"/mcp": "main"}),
    )
    connector = InMemoryConnector({"http://alpha/mcp": a, "http://beta/mcp": b})
    gw = SurfaceGateway(reg, "main", connector)

    res = anyio.run(gw.call_tool, "beta__echo", {"value": "hi"})
    assert res.isError is not True
    # The upstream CallToolResult is returned verbatim; the stub echoes its
    # plugin+tool, proving the call reached beta's echo (not alpha's).
    import json

    payload = json.loads(res.content[0].text)
    assert payload == {"plugin": "beta", "tool": "echo", "echo": "hi"}


def test_call_to_down_plugin_route_arounds():
    import pytest

    from snowline_platform.gateway import GatewayError

    a = make_stub_plugin("alpha", ["echo"])
    reg = _registry(
        PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"})
    )
    connector = InMemoryConnector({"http://alpha/mcp": a})
    gw = SurfaceGateway(reg, "main", connector)
    reg.set_status("alpha", PluginStatus.DOWN)

    # The down plugin is no longer an upstream of `main` -> clear error, no hang.
    with pytest.raises(GatewayError):
        anyio.run(gw.call_tool, "alpha__echo", {})
    # And it lists no tools.
    assert anyio.run(gw.list_tools) == []


def test_isolation_by_composition():
    """A tool a plugin maps ONLY onto `main` is absent from `shadow` — purely by
    composition (the gateway does no per-tool filtering)."""
    # `gov_main` carries record_decision; `gov_shadow` does NOT (separate server).
    gov_main = make_stub_plugin("governance", ["record_decision", "get_decision"])
    gov_shadow = make_stub_plugin("governance", ["add_node", "get_decision"])
    reg = _registry(
        PluginManifest(
            name="governance",
            base_url="http://gov",
            surfaces={"/mcp": "main", "/shadow/mcp": "shadow"},
        )
    )
    connector = InMemoryConnector(
        {"http://gov/mcp": gov_main, "http://gov/shadow/mcp": gov_shadow}
    )

    main_tools = {t.name for t in anyio.run(SurfaceGateway(reg, "main", connector).list_tools)}
    shadow_tools = {
        t.name for t in anyio.run(SurfaceGateway(reg, "shadow", connector).list_tools)
    }

    assert "governance__record_decision" in main_tools
    assert "governance__record_decision" not in shadow_tools  # the isolation property
    assert "governance__add_node" in shadow_tools
    assert "governance__add_node" not in main_tools

    # And calling record_decision on the shadow surface cannot mutate the real
    # graph: it's not listed there, and routing it reaches only the shadow
    # upstream (which has no such tool) -> an error result, never a real write.
    res = anyio.run(
        SurfaceGateway(reg, "shadow", connector).call_tool,
        "governance__record_decision",
        {},
    )
    assert res.isError is True
