"""Mount the gateway's aggregated MCP surfaces on the platform app.

For each NAMED platform surface the registry knows about (`main`, `shadow`, …)
the gateway serves ONE streamable-HTTP MCP endpoint that aggregates every
registered plugin-surface mapped to it (gateway.md §2). This module is the glue
between `gateway.build_surface_server` (the per-surface low-level MCP server) and
the FastAPI/Starlette app: it builds a `StreamableHTTPSessionManager` per surface
and mounts it at the surface's platform route, behind the existing trust gate.

Surface → route convention: ``main`` is the daily-driver surface at ``/mcp``;
every other named surface ``X`` is mounted at ``/X/mcp`` (so ``shadow`` → matches
``/shadow/mcp``, mirroring how the governance plugin lays out its own paths).
More-specific routes are mounted before ``/mcp`` so Starlette matches them first.

The session managers' `run()` is a required-for-lifespan async context (the
StreamableHTTP manager owns the task group serving sessions); they are entered in
the platform app's lifespan and torn down on shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Receive, Scope, Send

from snowline_platform.gateway import (
    StreamableHttpConnector,
    UpstreamConnector,
    build_surface_server,
)
from snowline_platform.registry import PluginRegistry

# The named surfaces the platform always exposes. `main` is the composed
# daily-driver surface; `shadow` is the isolated speculation surface (decision
# 8a7f0a11). A surface is mounted whether or not a plugin currently maps onto it
# — an empty surface simply lists no tools — so the route exists before the first
# plugin registers (hot-plug). More surfaces can be added here as plugins
# introduce them.
DEFAULT_SURFACES: tuple[str, ...] = ("main", "shadow")

# DNS-rebinding protection off on the streamable-HTTP transport: the gateway sits
# behind the platform trust gate (reached on the tailnet), matching the
# governance plugin's own surfaces. Can be tightened via env in a later
# increment if the platform is ever exposed beyond the tailnet.
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def surface_route(surface: str) -> str:
    """The platform route a named surface is served at: ``main`` → ``/mcp``,
    any other ``X`` → ``/X/mcp``."""
    return "/mcp" if surface == "main" else f"/{surface}/mcp"


class _SurfaceMount:
    """Holds a named surface's session manager + ASGI handler so the lifespan can
    enter its `run()` and the app can mount its `handle_request`."""

    def __init__(
        self,
        surface: str,
        registry: PluginRegistry,
        connector: UpstreamConnector,
    ) -> None:
        self.surface = surface
        self.route = surface_route(surface)
        server = build_surface_server(registry, surface, connector)
        # stateless=True: the gateway holds no per-session server state of its own
        # (each list/call re-discovers upstreams + opens a fresh upstream
        # session), so a stateless transport is the honest model and avoids
        # session-affinity bookkeeping across the proxy.
        self._manager = StreamableHTTPSessionManager(
            app=server,
            stateless=True,
            security_settings=_SECURITY,
        )

    async def asgi(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._manager.handle_request(scope, receive, send)

    def run(self):
        return self._manager.run()


def build_surface_mounts(
    registry: PluginRegistry,
    connector: UpstreamConnector | None = None,
    surfaces: tuple[str, ...] = DEFAULT_SURFACES,
) -> list[_SurfaceMount]:
    """One `_SurfaceMount` per named surface. `connector` defaults to the
    production streamable-HTTP connector; tests inject an in-memory one."""
    conn = connector or StreamableHttpConnector()
    return [_SurfaceMount(s, registry, conn) for s in surfaces]


def mount_gateway(app, mounts: list[_SurfaceMount]) -> None:
    """Mount each surface's ASGI handler on the FastAPI/Starlette `app`.

    Routes are added MOST-SPECIFIC first (e.g. ``/shadow/mcp`` before ``/mcp``)
    so Starlette's first-match routing picks the right surface; ``/mcp`` (the
    `main` surface) is mounted last. Mounting (vs a single route) lets the
    streamable-HTTP transport own the sub-path (GET/POST/DELETE + the session
    sub-routes it manages)."""
    ordered = sorted(mounts, key=lambda m: len(m.route), reverse=True)
    for mount in ordered:
        app.mount(mount.route, mount.asgi)


@asynccontextmanager
async def gateway_lifespan(
    mounts: list[_SurfaceMount],
) -> AsyncIterator[None]:
    """Enter every surface session manager's `run()` for the app lifespan."""
    async with AsyncExitStack() as stack:
        for mount in mounts:
            await stack.enter_async_context(mount.run())
        yield
