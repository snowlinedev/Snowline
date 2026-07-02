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


# --- Per-surface plugin allowlists (issues #36 + #38) -------------------------
#
# REAL-shaped manifests throughout (governance maps main+shadow, pm maps main —
# nothing maps a `core` surface natively): issue #38 was masked by fake
# `{"/core/mcp": "core"}` manifests, a shape no real plugin has, so the #37
# tests passed while the live `/core/mcp` served ZERO tools.

_GOV_SURFACES = {"/mcp": "main", "/shadow/mcp": "shadow"}
_PM_SURFACES = {"/mcp": "main"}


def test_allowlist_filters_discovery_by_plugin_name():
    """A surface handed an allowlist aggregates ONLY the listed plugins (pm is
    filtered out even though it, too, maps `main` — the projection source); a
    surface handed None (no allowlist — the default) still allows all. The
    allowlist arrives as a frozen argument (parsed once at mount time by
    `build_surface_mounts`), never re-read from the env here."""
    reg = _registry(
        PluginManifest(
            name="governance", base_url="http://gov", surfaces=_GOV_SURFACES
        ),
        PluginManifest(name="pm", base_url="http://pm", surfaces=_PM_SURFACES),
    )

    # `core` is constrained to governance; pm is filtered out.
    assert {
        u.plugin_name
        for u in discover_upstreams(reg, "core", frozenset({"governance"}))
    } == {"governance"}
    # No allowlist (None) -> allow-all -> both plugins still compose.
    assert {u.plugin_name for u in discover_upstreams(reg, "main", None)} == {
        "governance",
        "pm",
    }


def test_allowlisted_surface_projects_root_surface_mapping():
    """Issue #38, the live bug: `core=governance` with the REAL manifest shape
    (governance maps only main+shadow — nothing maps `core` natively) must
    project governance's ROOT_SURFACE (`main`) mapping onto `core`, not serve an
    empty surface. The projected upstream is governance's `/mcp` plugin-path."""
    from snowline_platform.gateway import ROOT_SURFACE

    reg = _registry(
        PluginManifest(
            name="governance", base_url="http://gov", surfaces=_GOV_SURFACES
        ),
        PluginManifest(name="pm", base_url="http://pm", surfaces=_PM_SURFACES),
    )

    ups = discover_upstreams(reg, "core", frozenset({"governance"}))
    assert [(u.plugin_name, u.plugin_path) for u in ups] == [
        ("governance", "/mcp")
    ]
    # The projection source is the plugin's ROOT_SURFACE mapping, by constant.
    assert _GOV_SURFACES["/mcp"] == ROOT_SURFACE


def test_allowlisted_plugin_with_native_mapping_prefers_it():
    """A plugin that DOES declare a native mapping for the allowlisted surface
    keeps it — projection is the fallback, not an override — and its main
    mapping is NOT also projected (no duplicate upstream, so the issue-#22
    duplicate-path guard is never tripped by projection)."""
    reg = _registry(
        PluginManifest(
            name="governance",
            base_url="http://gov",
            surfaces={"/mcp": "main", "/core/mcp": "core"},
        ),
    )

    ups = discover_upstreams(reg, "core", frozenset({"governance"}))
    # Exactly ONE upstream: the native /core/mcp — /mcp (main) is not projected.
    assert [(u.plugin_name, u.plugin_path) for u in ups] == [
        ("governance", "/core/mcp")
    ]


def test_allowlisted_plugin_with_neither_mapping_contributes_nothing():
    """An allowlisted plugin with NEITHER a native mapping for the surface NOR a
    ROOT_SURFACE mapping simply contributes nothing — no error, no phantom
    upstream."""
    reg = _registry(
        PluginManifest(
            name="shadowonly",
            base_url="http://so",
            surfaces={"/shadow/mcp": "shadow"},
        ),
    )
    assert discover_upstreams(reg, "core", frozenset({"shadowonly"})) == []


def test_unallowlisted_surface_keeps_pure_manifest_semantics():
    """CRITICAL isolation property: a surface WITHOUT an allowlist (None) never
    projects — `shadow` serves ONLY native shadow mappings. Governance's main
    mapping must not leak onto shadow via any projection path, and pm (which
    maps only main) contributes nothing to shadow at all."""
    reg = _registry(
        PluginManifest(
            name="governance", base_url="http://gov", surfaces=_GOV_SURFACES
        ),
        PluginManifest(name="pm", base_url="http://pm", surfaces=_PM_SURFACES),
    )

    shadow = discover_upstreams(reg, "shadow", None)
    assert [(u.plugin_name, u.plugin_path) for u in shadow] == [
        ("governance", "/shadow/mcp")
    ]


def test_allowlist_star_parses_to_allow_all(monkeypatch):
    """`main=*` parses to the None sentinel, which discovery treats exactly like
    an unlisted surface — proving the parse→discover handoff for `*`."""
    from snowline_platform import config

    reg = _registry(
        PluginManifest(name="governance", base_url="http://gov", surfaces={"/mcp": "main"}),
        PluginManifest(name="pm", base_url="http://pm", surfaces={"/mcp": "main"}),
    )
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", "main=*")
    allowlist = config.surface_plugins()["main"]
    assert allowlist is None
    assert {
        u.plugin_name for u in discover_upstreams(reg, "main", allowlist)
    } == {"governance", "pm"}


def test_allowlist_projection_tools_and_routing_end_to_end():
    """Issues #36+#38 at the tool level, with REAL-shaped manifests: on
    `core=governance` the gateway serves governance's projected MAIN tools
    (record_decision reaches the real /mcp upstream), pm's tools are absent AND
    unroutable, `main` (no allowlist) keeps both plugins, and `shadow` (no
    allowlist) serves ONLY governance's native shadow tools — main tools do not
    leak there via projection."""
    import pytest

    from snowline_platform.gateway import GatewayError

    gov_main = make_stub_plugin("governance", ["record_decision"])
    gov_shadow = make_stub_plugin("governance", ["add_node"])
    pm = make_stub_plugin("pm", ["create_work_item"])
    reg = _registry(
        PluginManifest(
            name="governance", base_url="http://gov", surfaces=_GOV_SURFACES
        ),
        PluginManifest(name="pm", base_url="http://pm", surfaces=_PM_SURFACES),
    )
    connector = InMemoryConnector(
        {
            "http://gov/mcp": gov_main,
            "http://gov/shadow/mcp": gov_shadow,
            "http://pm/mcp": pm,
        }
    )
    core = SurfaceGateway(reg, "core", connector, frozenset({"governance"}))
    core_tools = {t.name for t in anyio.run(core.list_tools)}
    # Projection: governance's MAIN tools appear on core; nothing pm__.
    assert core_tools == {"governance__record_decision"}
    # And a call routes to the real main upstream (the projected plugin-path).
    res = anyio.run(core.call_tool, "governance__record_decision", {"value": "x"})
    assert res.isError is not True
    import json

    payload = json.loads(res.content[0].text)
    assert payload == {"plugin": "governance", "tool": "record_decision", "echo": "x"}
    # pm's tool is unroutable on `core` (filtered) -> clear error, not a misroute.
    with pytest.raises(GatewayError):
        anyio.run(core.call_tool, "pm__create_work_item", {})

    # `main` (no allowlist) keeps BOTH plugins' tools.
    main = SurfaceGateway(reg, "main", connector, None)
    main_tools = {t.name for t in anyio.run(main.list_tools)}
    assert main_tools == {"governance__record_decision", "pm__create_work_item"}

    # `shadow` (no allowlist) is UNCHANGED: native shadow tools only — no
    # projected main tools, nothing from pm.
    shadow = SurfaceGateway(reg, "shadow", connector, None)
    shadow_tools = {t.name for t in anyio.run(shadow.list_tools)}
    assert shadow_tools == {"governance__add_node"}


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


def test_multi_path_on_one_surface_rejects_duplicate(caplog):
    """Issue #22: a plugin mapping TWO paths onto the SAME surface is a config
    error — discovery keeps the lexicographically-first path, drops the rest with
    a warning, so list_tools never advertises unroutable tools (policy (a))."""
    import logging

    reg = _registry(
        PluginManifest(
            name="dup",
            base_url="http://dup",
            # Both paths map to `main`; namespace is keyed by plugin only, so the
            # two are indistinguishable + only one is routable.
            surfaces={"/mcp": "main", "/admin": "main"},
        )
    )
    with caplog.at_level(logging.WARNING, logger="snowline_platform.gateway"):
        ups = discover_upstreams(reg, "main")

    # Exactly one upstream survives — the lexicographically-first path.
    assert [(u.plugin_name, u.plugin_path) for u in ups] == [("dup", "/admin")]
    # And it was loud about it.
    assert any(
        "maps multiple paths" in rec.message and "main" in rec.message
        for rec in caplog.records
    )


def test_multi_path_no_silent_misroute_end_to_end():
    """Issue #22, behavior: with the dup path rejected, every tool list_tools
    advertises is routable by call_tool (no GatewayError for an advertised
    tool) — the foot-gun is closed, not just warned about."""
    # `/admin` (kept path) carries `ping`; `/mcp` (dropped path) carries `secret`.
    admin = make_stub_plugin("dup", ["ping"])
    mcp = make_stub_plugin("dup", ["secret"])
    reg = _registry(
        PluginManifest(
            name="dup",
            base_url="http://dup",
            surfaces={"/mcp": "main", "/admin": "main"},
        )
    )
    connector = InMemoryConnector(
        {"http://dup/admin": admin, "http://dup/mcp": mcp}
    )
    gw = SurfaceGateway(reg, "main", connector)

    advertised = {t.name for t in anyio.run(gw.list_tools)}
    # Only the KEPT path's tool is advertised — the dropped path's `secret` is not.
    assert advertised == {"dup__ping"}
    # And the one advertised tool routes cleanly (no silent misroute / error).
    res = anyio.run(gw.call_tool, "dup__ping", {"value": "ok"})
    assert res.isError is not True


def test_list_tools_concurrent_merges_all():
    """Issue #23: list_tools fans out concurrently and merges ALL upstreams'
    tools in stable name-sorted order."""
    a = make_stub_plugin("alpha", ["a1", "a2"])
    b = make_stub_plugin("beta", ["b1"])
    c = make_stub_plugin("gamma", ["c1", "c2", "c3"])
    reg = _registry(
        PluginManifest(name="alpha", base_url="http://a", surfaces={"/mcp": "main"}),
        PluginManifest(name="beta", base_url="http://b", surfaces={"/mcp": "main"}),
        PluginManifest(name="gamma", base_url="http://g", surfaces={"/mcp": "main"}),
    )
    connector = InMemoryConnector(
        {"http://a/mcp": a, "http://b/mcp": b, "http://g/mcp": c}
    )
    gw = SurfaceGateway(reg, "main", connector)

    tools = anyio.run(gw.list_tools)
    names = [t.name for t in tools]
    # Stable order: upstreams are name-sorted, tools in each upstream's own order.
    assert names == [
        "alpha__a1",
        "alpha__a2",
        "beta__b1",
        "gamma__c1",
        "gamma__c2",
        "gamma__c3",
    ]


def test_list_tools_one_failing_upstream_does_not_blank_others():
    """Issue #23: one upstream raising on connect/list is route-around-ed in its
    own task — the others' tools still merge (no blanked surface)."""
    good = make_stub_plugin("good", ["ok"])
    reg = _registry(
        PluginManifest(name="good", base_url="http://good", surfaces={"/mcp": "main"}),
        PluginManifest(name="bad", base_url="http://bad", surfaces={"/mcp": "main"}),
    )
    # `bad`'s URL is absent from the connector map -> InMemoryConnector raises
    # ConnectionError on connect, standing in for an unreachable upstream.
    connector = InMemoryConnector({"http://good/mcp": good})
    gw = SurfaceGateway(reg, "main", connector)

    names = {t.name for t in anyio.run(gw.list_tools)}
    assert names == {"good__ok"}


def test_list_tools_slow_upstream_times_out_and_others_succeed():
    """Issue #23: a slow (not DOWN) upstream is bounded by LIST_TIMEOUT and
    route-around-ed; the fast upstream still returns. With concurrency the slow
    one doesn't block the fast one."""
    from contextlib import asynccontextmanager

    fast = make_stub_plugin("fast", ["go"])

    class _SlowOrFast(InMemoryConnector):
        @asynccontextmanager
        async def connect(self, upstream):
            if upstream.plugin_name == "slow":
                # Hang well past LIST_TIMEOUT; fail_after must cancel us.
                await anyio.sleep(60)
                raise AssertionError("slow upstream should have been cancelled")
                yield  # pragma: no cover
            else:
                async with super().connect(upstream) as session:
                    yield session

    reg = _registry(
        PluginManifest(name="fast", base_url="http://fast", surfaces={"/mcp": "main"}),
        PluginManifest(name="slow", base_url="http://slow", surfaces={"/mcp": "main"}),
    )
    connector = _SlowOrFast({"http://fast/mcp": fast})
    gw = SurfaceGateway(reg, "main", connector)
    gw.LIST_TIMEOUT = 0.1  # keep the test fast

    async def _run():
        with anyio.fail_after(5):  # the WHOLE list must not hang on the slow one
            return await gw.list_tools()

    names = {t.name for t in anyio.run(_run)}
    assert names == {"fast__go"}


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
