"""The platform GATEWAY — composes registered plugins into named MCP surfaces.

This is what makes Snowline a *platform* (gateway.md, architecture §"surface
composition", decision-derived model 70b415fd): the platform exposes **named MCP
surfaces** (`main`, `shadow`, …); each plugin's manifest **maps its own surfaces**
onto them (governance: ``{"/mcp": "main", "/shadow/mcp": "shadow"}``); and for
each named surface the gateway **aggregates** every plugin-surface mapped to it
into the one MCP endpoint the client connects to.

The gateway never imports plugin code — it reads the in-memory `PluginRegistry`
and talks to each plugin only over MCP/streamable-HTTP at ``base_url + plugin``
(local OR cross-tailnet). Composition is the whole job:

  - **Discovery** — a named surface's upstreams = every registered plugin whose
    ``manifest.surfaces`` maps one of its plugin-paths to that surface. A plugin
    whose registry ``status`` is ``DOWN`` is SKIPPED (health-aware route-around,
    §4); ``UNKNOWN`` is treated as routable (the health poller that sets ``UP``
    is a later PR).
  - **list_tools** — the merged union of the upstreams' tool lists, with each
    tool NAMESPACED by its owning plugin (``governance.record_decision``). See
    `namespaced_name` for the collision policy.
  - **call_tool** — un-namespace ``plugin.tool`` → route to that plugin's session
    and return the upstream `CallToolResult` verbatim (content + structured +
    isError preserved).
  - **Isolation is plugin-side and structural** — the gateway does NO per-tool
    filtering. A tool appears on a named surface ONLY because a plugin mapped a
    surface carrying it onto that named surface; ``record_decision`` lands on
    `main` and is absent from `shadow` purely by composition (decision 8a7f0a11).

Connections are PER-REQUEST (open `ClientSession`, list/call, close) — correct
first cut; pooling/caching is a deferred perf follow-up (gateway.md pragmatics).
The upstream connection is abstracted behind `UpstreamConnector` so tests can
wire an in-memory MCP plugin without standing up HTTP, while production uses the
streamable-HTTP client.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server

from snowline_platform.registry import PluginRegistry, PluginStatus

log = logging.getLogger("snowline_platform.gateway")

# How a namespaced gateway tool name (``plugin__tool``) is split. We delimit with
# a DOUBLE UNDERSCORE — the ``mcp__<server>__<tool>`` ecosystem convention — for a
# load-bearing reason: the gateway's tools are consumed by Claude (the integration
# runtime, decision 61e50214), and the Anthropic tool-name charset is
# ``^[a-zA-Z0-9_-]{1,64}$`` — a DOT is rejected there even though the MCP
# server-side regex (``^[A-Za-z0-9._-]{1,128}$``) permits it. So a dot separator
# passes every in-repo test yet breaks the one real consumer. ``__`` is in-charset
# and unambiguous: plugin names are url-safe slugs (``[a-z0-9][a-z0-9-]*`` — no
# underscores at all), so ``__`` never occurs inside a plugin name and the FIRST
# ``__`` cleanly separates the plugin prefix from the upstream tool name (which may
# itself contain ``_`` or even ``__``; we split on the first to stay robust).
_NS_SEP = "__"


def namespaced_name(plugin_name: str, tool_name: str) -> str:
    """The gateway-visible name for an upstream tool.

    **Collision policy = namespace-by-plugin** (gateway.md §7 "Open"): the
    gateway prefixes every upstream tool with ``<plugin>__``. Two plugins on the
    same named surface that both expose ``record_decision`` therefore appear as
    ``governance__record_decision`` and ``other__record_decision`` — no
    collision, no silent shadowing, and the prefix is the routing key (call_tool
    reverses it). This is the safer of the two options the spec floated (vs
    first-wins-with-warning), and it is unambiguous because plugin names are
    url-safe slugs containing no underscore. The ``__`` delimiter keeps the name
    inside the Anthropic tool-name charset so Claude — the consumer — accepts it.
    """
    return f"{plugin_name}{_NS_SEP}{tool_name}"


def split_namespaced(name: str) -> tuple[str, str]:
    """Reverse `namespaced_name`: ``plugin__tool`` → ``(plugin, tool)``.

    Splits on the FIRST ``__``, so an upstream tool name that itself contained a
    ``__`` would survive in the suffix. Raises `ValueError` for an un-namespaced
    name (no ``__``) — that is a routing bug, surfaced as a clear call_tool error
    rather than a silent misroute."""
    plugin, sep, tool = name.partition(_NS_SEP)
    if not sep or not plugin or not tool:
        raise ValueError(
            f"tool name {name!r} is not gateway-namespaced (expected "
            f"'<plugin>{_NS_SEP}<tool>')"
        )
    return plugin, tool


@dataclass(frozen=True)
class Upstream:
    """One plugin-surface mapped onto a named platform surface — the address the
    gateway connects to. `plugin_name` is the namespace prefix + routing key;
    `base_url` + `plugin_path` is the streamable-HTTP endpoint."""

    plugin_name: str
    base_url: str
    plugin_path: str

    @property
    def url(self) -> str:
        return f"{self.base_url}{self.plugin_path}"


# An UpstreamConnector.connect() yields a *connected, initialized* ClientSession
# for the duration of an `async with`, then tears it down. Per-request: one
# connect per list/call. Abstracted so tests can inject an in-memory transport.
class AbstractAsyncCM(Protocol):
    async def __aenter__(self): ...
    async def __aexit__(self, *exc) -> bool | None: ...


class UpstreamConnector(Protocol):
    """Opens a `ClientSession` to one upstream plugin-surface.

    Production: `StreamableHttpConnector` (HTTP to ``base_url + plugin_path``).
    Tests: an in-memory connector wired to a real `FastMCP`/`Server` so the
    aggregation + routing path is exercised without standing up HTTP."""

    def connect(self, upstream: Upstream) -> AbstractAsyncCM[ClientSession]: ...


class StreamableHttpConnector:
    """Production connector: streamable-HTTP to ``base_url + plugin_path``.

    Opens a fresh `streamablehttp_client` transport + `ClientSession` per call
    and initializes it. The streaming + session semantics live entirely in the
    mcp client/transport: we open the transport, run one request (list or call)
    on the initialized session, and the upstream's response — including a
    streamed/chunked tool result — is reassembled by `ClientSession` into the
    `ListToolsResult` / `CallToolResult` we return. Per-request connect keeps
    the first cut correct; pooling is a deferred perf follow-up."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    @asynccontextmanager
    async def connect(self, upstream: Upstream) -> AsyncIterator[ClientSession]:
        import httpx

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout), follow_redirects=True
        ) as http_client:
            async with streamable_http_client(
                upstream.url, http_client=http_client
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


class GatewayError(Exception):
    """A composition/routing failure the gateway surfaces as a clear MCP error
    rather than hanging (e.g. a tool routed to a plugin not on this surface, or
    a down/unreachable upstream)."""


def discover_upstreams(
    registry: PluginRegistry, surface: str
) -> list[Upstream]:
    """Every registered plugin-surface mapped onto the named `surface`, SKIPPING
    plugins whose status is `DOWN` (health-aware route-around, gateway.md §4).

    A plugin's effective surface map is `manifest.surfaces`, defaulting (per the
    manifest contract) to ``{mcp_path: "main"}`` when empty — so a plugin that
    declares no surfaces still composes onto `main`. `UNKNOWN` is routable (the
    poller that promotes to `UP` is a later PR); only an explicit `DOWN` is
    skipped, so the gateway route-arounds a dead upstream instead of hanging on
    it. Ordered by plugin name for a stable merged tool list."""
    upstreams: list[Upstream] = []
    for entry in registry.list():
        if entry.status is PluginStatus.DOWN:
            log.info(
                "gateway: skipping down plugin %r for surface %r",
                entry.manifest.name,
                surface,
            )
            continue
        manifest = entry.manifest
        surface_map = manifest.surfaces or {manifest.mcp_path: "main"}
        for plugin_path, named in surface_map.items():
            if named == surface:
                upstreams.append(
                    Upstream(
                        plugin_name=manifest.name,
                        base_url=manifest.base_url,
                        plugin_path=plugin_path,
                    )
                )
    upstreams.sort(key=lambda u: (u.plugin_name, u.plugin_path))
    return upstreams


class SurfaceGateway:
    """Aggregates the upstreams of ONE named surface into list/call handlers.

    Stateless beyond its `registry` + `connector` + `surface` name: it
    re-discovers upstreams on every `list_tools`/`call_tool`, so a plugin
    registered/unregistered/marked-down at runtime is reflected on the next
    request without a restart (hot-plug, architecture §3). Discovery is live, so
    the health-aware route-around is too."""

    def __init__(
        self,
        registry: PluginRegistry,
        surface: str,
        connector: UpstreamConnector,
    ) -> None:
        self._registry = registry
        self._surface = surface
        self._connector = connector

    @property
    def surface(self) -> str:
        return self._surface

    async def list_tools(self) -> list[types.Tool]:
        """Merged union of the upstreams' tool lists, each tool NAMESPACED by its
        owning plugin. A per-upstream failure is logged + skipped (route-around)
        rather than failing the whole list — one dead plugin doesn't blank the
        surface."""
        merged: list[types.Tool] = []
        for upstream in discover_upstreams(self._registry, self._surface):
            try:
                async with self._connector.connect(upstream) as session:
                    result = await session.list_tools()
            except Exception as exc:  # route-around a failing upstream
                log.warning(
                    "gateway: list_tools failed for upstream %s on surface %r: "
                    "%s",
                    upstream.url,
                    self._surface,
                    exc,
                )
                continue
            for tool in result.tools:
                merged.append(_namespace_tool(upstream.plugin_name, tool))
        return merged

    async def call_tool(
        self, name: str, arguments: dict | None
    ) -> types.CallToolResult:
        """Route a ``plugin.tool`` call to its owning upstream and return the
        upstream `CallToolResult` verbatim (content + structuredContent + isError
        preserved). The owning plugin is resolved from the namespace prefix and
        must be a live (non-down) upstream of THIS surface — so a tool the plugin
        only mapped onto another surface (e.g. `record_decision` on `main`) is
        unroutable here, which is the isolation property surfaced as an error."""
        plugin_name, tool_name = split_namespaced(name)
        upstreams = {
            u.plugin_name: u
            for u in discover_upstreams(self._registry, self._surface)
        }
        upstream = upstreams.get(plugin_name)
        if upstream is None:
            raise GatewayError(
                f"no live plugin {plugin_name!r} on surface {self._surface!r} "
                f"for tool {name!r} (unregistered, down, or not mapped here)"
            )
        async with self._connector.connect(upstream) as session:
            return await session.call_tool(tool_name, arguments or {})


def _namespace_tool(plugin_name: str, tool: types.Tool) -> types.Tool:
    """A copy of `tool` with its name namespaced by `plugin_name`; description,
    schema, and annotations are carried verbatim (the ``<plugin>__`` prefix on the
    name is what keeps two same-named tools distinct + legible in the merged
    list)."""
    return tool.model_copy(
        update={"name": namespaced_name(plugin_name, tool.name)}
    )


def build_surface_server(
    registry: PluginRegistry,
    surface: str,
    connector: UpstreamConnector,
) -> Server:
    """A low-level `mcp.server.lowlevel.Server` for one named surface, wired to a
    `SurfaceGateway`'s list/call handlers.

    We use the LOW-LEVEL Server (not FastMCP) deliberately: the gateway has no
    statically-known tool set — its tools are whatever the live upstreams expose
    — so a dynamic `list_tools` handler + a pass-through `call_tool` is exactly
    the shape, and the low-level server lets `call_tool` return an upstream
    `CallToolResult` UNMODIFIED (FastMCP would re-wrap content/structured). The
    Server is served over streamable-HTTP by a `StreamableHTTPSessionManager`
    mounted on the platform app (see `gateway_app`)."""
    gateway = SurfaceGateway(registry, surface, connector)
    server: Server = Server(f"snowline-{surface}")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return await gateway.list_tools()

    # validate_input=False: the OWNING plugin validates against its own
    # inputSchema; the gateway is a pass-through and must not double-validate
    # (and can't, without re-deriving the upstream schema per call). Returning
    # the upstream CallToolResult directly preserves isError + structuredContent.
    @server.call_tool(validate_input=False)
    async def _call_tool(
        name: str, arguments: dict
    ) -> types.CallToolResult:
        try:
            return await gateway.call_tool(name, arguments)
        except GatewayError as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))],
                isError=True,
            )

    return server


# The handler factory is also exposed for tests that drive the Server directly.
__all__ = [
    "Upstream",
    "UpstreamConnector",
    "StreamableHttpConnector",
    "SurfaceGateway",
    "GatewayError",
    "discover_upstreams",
    "build_surface_server",
    "namespaced_name",
    "split_namespaced",
]
