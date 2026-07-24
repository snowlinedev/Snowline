"""The platform's OWN MCP tool surface, served by the platform registering ITSELF
as an upstream (governance decision 0503fff0): the native scope + milestone verbs
(scope-namespace.md §4, milestones.md §5) compose onto `main` through the
ORDINARY gateway aggregation path — no special-casing in the aggregator.

Two layers, mirroring the existing gateway tests:

  - **Composition unit** (`discover_upstreams`, no DB): the seeded `platform`
    self-entry is an ordinary manifest — it composes onto `main`, is ABSENT from
    an isolation surface like `shadow`, and PROJECTS onto an explicitly-
    allowlisted surface via its ROOT_SURFACE mapping exactly like any plugin
    (gateway.md §2a / issue #38).

  - **End-to-end over the REAL streamable-HTTP transport** (mirrors
    test_gateway_app): the platform's tool FastMCP is wired at the connector seam
    (in-memory MCP transport) at the platform's own loopback URL, so the COMPOSED
    gateway surface is exercised without standing up HTTP. `list_tools` shows the
    tools namespaced `platform__*`; a routed `platform__resolve_milestone`
    genuinely round-trips into the (disposable) platform DB — including a MISS
    whose near-miss suggestions survive into the tool error.
"""

from __future__ import annotations

import json

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from snowline_platform import config, milestones, scopes
from snowline_platform.app import create_app
from snowline_platform.db import session_scope
from snowline_platform.gateway import discover_upstreams
from snowline_platform.platform_tools import (
    PLATFORM_MCP_PATH,
    PLATFORM_PLUGIN_NAME,
    build_platform_tools_surface,
    platform_self_manifest,
)
from snowline_platform.registry import PluginRegistry
from snowline_platform.trust import Principal, TrustResolver

from ._gateway_helpers import InMemoryConnector, make_stub_plugin

# Every tool the platform surface exposes, namespaced (scope-namespace.md §4 +
# milestones.md §5). The `platform__` prefix is the `<plugin>__<tool>` convention
# applied to the self-entry's registry name.
_SCOPE_TOOLS = {
    "list_scopes",
    "resolve_scope",
    "scope_tree",
    "scope_ancestors",
    "create_scope",
    "update_scope",
}
_MILESTONE_TOOLS = {
    "create_milestone",
    "resolve_milestone",
    "list_milestones",
    "get_milestone",
    "milestone_transitions",
    "activate_milestone",
    "achieve_milestone",
    "cancel_milestone",
}
_ALL_PLATFORM_TOOLS = {f"platform__{t}" for t in _SCOPE_TOOLS | _MILESTONE_TOOLS}


class _AlwaysTrust:
    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _platform_upstream_url() -> str:
    """The URL the gateway dials for the self-entry — its loopback base_url +
    `/platform/mcp` (i.e. `Upstream.url` for the self manifest)."""
    return config.platform_self_url() + PLATFORM_MCP_PATH


def _connector_with_platform(extra: dict | None = None) -> InMemoryConnector:
    """An in-memory connector wiring the platform self-entry URL to a fresh
    platform tool surface (plus any `extra` plugin servers)."""
    servers = {_platform_upstream_url(): build_platform_tools_surface()}
    servers.update(extra or {})
    return InMemoryConnector(servers)


def _app(connector: InMemoryConnector, registry: PluginRegistry | None = None):
    # create_app seeds the platform self-entry into whatever registry it uses,
    # so a fresh registry still composes the platform upstream.
    return create_app(
        resolver=TrustResolver([_AlwaysTrust()]),
        registry=registry or PluginRegistry(),
        migrate_on_startup=False,
        connector=connector,
    )


def _asgi_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://platform",
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    )


async def _run_against_surface(app, route, fn):
    from snowline_platform.gateway_app import gateway_lifespan

    async with gateway_lifespan(app.state.gateway_mounts):
        async with _asgi_client(app) as http_client:
            async with streamable_http_client(
                f"http://platform{route}", http_client=http_client
            ) as (read, write, _sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await fn(session)


# --- composition unit (discover_upstreams) ----------------------------------


def test_self_entry_composes_on_main_and_is_absent_from_shadow():
    """The seeded `platform` manifest composes onto `main` (its only surface
    mapping) and NOWHERE else: an isolation surface like `shadow` gets no platform
    upstream, purely by composition (the self-entry maps only `main`)."""
    reg = PluginRegistry()
    reg.upsert(platform_self_manifest())

    main = discover_upstreams(reg, "main")
    assert [u.plugin_name for u in main] == [PLATFORM_PLUGIN_NAME]
    assert main[0].plugin_path == PLATFORM_MCP_PATH
    assert main[0].base_url == config.platform_self_url()

    assert discover_upstreams(reg, "shadow") == []


def test_self_entry_projects_onto_allowlisted_surface_like_any_plugin():
    """gateway.md §2a / issue #38: the platform self-entry has no native mapping
    for an operator-invented surface, so on an ALLOWLISTED surface it behaves like
    any plugin — its ROOT_SURFACE (`main`) mapping PROJECTS when the allowlist
    names it, and it is filtered out when the allowlist does not. Zero
    special-casing: the same projection code as governance/pm produces this."""
    reg = PluginRegistry()
    reg.upsert(platform_self_manifest())

    # `platform` in the allowlist → its main mapping projects onto `core`.
    projected = discover_upstreams(reg, "core", allowlist=frozenset({"platform"}))
    assert [u.plugin_name for u in projected] == [PLATFORM_PLUGIN_NAME]

    # `platform` absent from the allowlist → filtered out (no projection leak).
    assert discover_upstreams(reg, "core", allowlist=frozenset({"governance"})) == []


def test_self_entry_does_not_break_plugin_listing_or_health_manifest():
    """The self-entry is a PLAIN registry entry: it lists in GET /plugins next to
    real plugins, and carries a sane health-checkable manifest (default
    `/health` on its own loopback, NOT the tool path — so the poller checks the
    platform's own liveness endpoint like any plugin)."""
    from starlette.testclient import TestClient

    reg = PluginRegistry()
    app = _app(_connector_with_platform(), registry=reg)
    client = TestClient(app)

    # A real plugin registers alongside; the listing carries both.
    client.post(
        "/plugins", json={"name": "governance", "base_url": "http://127.0.0.1:8801"}
    )
    listed = {p["name"]: p for p in client.get("/plugins").json()["plugins"]}
    assert {"platform", "governance"} <= set(listed)

    self_entry = listed["platform"]
    assert self_entry["status"] == "unknown"  # not yet health-checked
    assert self_entry["manifest"]["base_url"] == config.platform_self_url()
    assert self_entry["manifest"]["surfaces"] == {PLATFORM_MCP_PATH: "main"}
    # health_path stays the default /health (the platform's own liveness), and
    # health_url therefore points at the platform, not the tool app.
    assert self_entry["manifest"]["health_path"] == "/health"

    from snowline_platform.health import health_url

    manifest = platform_self_manifest()
    assert health_url(manifest) == config.platform_self_url() + "/health"


def test_self_entry_name_is_reserved_on_the_registration_surface():
    """The registration surface must not touch the self-entry: a POST with the
    `platform` name would hijack the `platform__*` tool namespace onto a foreign
    base_url, and a DELETE would silently drop every native tool until restart —
    both 409, and the seeded entry survives untouched."""
    from starlette.testclient import TestClient

    app = _app(_connector_with_platform())
    client = TestClient(app)

    hijack = client.post(
        "/plugins", json={"name": "platform", "base_url": "http://evil:9999"}
    )
    assert hijack.status_code == 409

    assert client.delete("/plugins/platform").status_code == 409

    listed = {p["name"]: p for p in client.get("/plugins").json()["plugins"]}
    assert listed["platform"]["manifest"]["base_url"] == config.platform_self_url()


# --- end-to-end over the composed streamable-HTTP surface --------------------


def test_composed_main_lists_platform_tools_namespaced():
    """Through the REAL streamable-HTTP `main` surface, every platform scope +
    milestone tool is listed, namespaced `platform__*` by the ordinary gateway
    prefix convention."""
    app = _app(_connector_with_platform())

    async def _list(session):
        return await session.list_tools()

    tools = anyio.run(_run_against_surface, app, "/mcp", _list)
    names = {t.name for t in tools.tools}
    assert _ALL_PLATFORM_TOOLS <= names, sorted(_ALL_PLATFORM_TOOLS - names)


def test_composed_main_merges_platform_with_a_plugin():
    """The self-entry aggregates WITH real plugins on `main` — no special path: a
    stub plugin's tools and the platform's native tools merge into one surface."""
    stub = make_stub_plugin("governance", ["record_decision"])
    reg = PluginRegistry()
    from snowline_platform.manifest import PluginManifest

    reg.upsert(
        PluginManifest(name="governance", base_url="http://gov", surfaces={"/mcp": "main"})
    )
    app = _app(
        _connector_with_platform({"http://gov/mcp": stub}), registry=reg
    )

    async def _list(session):
        return await session.list_tools()

    names = {t.name for t in anyio.run(_run_against_surface, app, "/mcp", _list).tools}
    assert "governance__record_decision" in names
    assert "platform__resolve_scope" in names


def test_shadow_surface_has_no_platform_tools():
    """The isolation surface `shadow` carries NO platform tools — the self-entry
    maps only `main`, so composition alone keeps them off `shadow`."""
    app = _app(_connector_with_platform())

    async def _list(session):
        return await session.list_tools()

    names = {t.name for t in anyio.run(_run_against_surface, app, "/shadow/mcp", _list).tools}
    assert not any(n.startswith("platform__") for n in names)


def test_resolve_milestone_round_trips_through_composed_surface(clean_db):
    """A routed `platform__resolve_milestone` genuinely reaches the platform's own
    DB through the composed surface and returns the canonical row incl.
    `resolved_via_alias` — the marquee: the platform serving its own tools by
    self-registration actually works end to end."""
    with session_scope() as s:
        scopes.create(s, slug="acme/widget", name="Widget", kind="project")
        milestones.create(s, anchor="acme/widget", name="v1-launch", outcome="ship it")

    app = _app(_connector_with_platform())

    async def _call(session):
        return await session.call_tool(
            "platform__resolve_milestone", {"ref": "acme/widget/v1-launch"}
        )

    res = anyio.run(_run_against_surface, app, "/mcp", _call)
    assert res.isError is not True
    payload = json.loads(res.content[0].text)
    assert payload["address"] == "acme/widget/v1-launch"
    assert payload["status"] == "planned"
    assert payload["resolved_via_alias"] is False


def test_resolve_milestone_miss_carries_suggestions_into_the_tool_error(clean_db):
    """A MISS through the composed surface is a tool error whose text carries the
    service's near-miss SUGGESTIONS (milestones bakes them into the message via
    `_suggestion_tail`), so they survive the round-trip to the agent — the
    error-mapping contract the tool descriptions promise."""
    with session_scope() as s:
        scopes.create(s, slug="acme/widget", name="Widget", kind="project")
        milestones.create(s, anchor="acme/widget", name="v1-launch")

    app = _app(_connector_with_platform())

    async def _call(session):
        return await session.call_tool(
            # A one-character typo on the name → a direct-address miss with a
            # near-miss suggestion.
            "platform__resolve_milestone",
            {"ref": "acme/widget/v1-launchh"},
        )

    res = anyio.run(_run_against_surface, app, "/mcp", _call)
    assert res.isError is True
    text = res.content[0].text
    assert "unknown milestone" in text
    assert "did you mean" in text and "v1-launch" in text


def test_create_scope_write_verb_round_trips_and_persists(clean_db):
    """A WRITE verb (`platform__create_scope`) routed through the composed surface
    mutates the platform DB — the write tools are live, not just reads."""
    app = _app(_connector_with_platform())

    async def _call(session):
        return await session.call_tool(
            "platform__create_scope",
            {"slug": "acme/gadget", "name": "Gadget", "kind": "project"},
        )

    res = anyio.run(_run_against_surface, app, "/mcp", _call)
    assert res.isError is not True
    payload = json.loads(res.content[0].text)
    assert payload["slug"] == "acme/gadget"

    # The write committed — the row is visible from a fresh session.
    with session_scope() as s:
        assert scopes.resolve(s, "acme/gadget") is not None
