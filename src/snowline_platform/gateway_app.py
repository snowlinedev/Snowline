"""Mount the gateway's aggregated MCP surfaces on the platform app.

For each NAMED platform surface the registry knows about (`main`, `shadow`, …)
the gateway serves ONE streamable-HTTP MCP endpoint that aggregates every
registered plugin-surface mapped to it (gateway.md §2). This module is the glue
between `gateway.build_surface_server` (the per-surface low-level MCP server) and
the FastAPI/Starlette app: it builds a `StreamableHTTPSessionManager` per surface
and mounts it at the surface's platform route, behind the existing trust gate.

Surface → route convention: the ROOT_SURFACE (``main``) is the daily-driver
surface at ``/mcp``; every other named surface ``X`` is mounted at ``/X/mcp``
(so ``shadow`` → matches ``/shadow/mcp``, mirroring how the governance plugin
lays out its own paths). Routes are mounted MOST-SPECIFIC first — by path-segment
depth, then length — so a route that is a path-prefix of another (e.g. ``/a/mcp``
vs ``/a/b/mcp``) can never shadow the deeper one under Starlette's first-match.

Configurable surface set, NOT manifest-derived. The surfaces are mounted at
create_app time, but plugins register LATER (they POST ``/plugins`` on their own
boot, after the platform is up), so at mount time the registry is empty — the
live surface set cannot be derived from registered manifests at startup. And a
``StreamableHTTPSessionManager.run()`` is once-per-instance, so a brand-new
surface can't be added at runtime by re-entering a mount. The set is therefore
read from config (`config.surfaces()` ← ``SNOWLINE_SURFACES``, default
``"main,shadow"``): adding a surface is a config change + a restart, not a code
edit. FUTURE: runtime dynamic-add of a surface (mount + a fresh lifespan-scoped
session manager within the running app) is out of scope here, gated on the
run()-once constraint.

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

from snowline_platform import config
from snowline_platform.gateway import (
    ROOT_SURFACE,
    StreamableHttpConnector,
    UpstreamConnector,
    build_surface_server,
)
from snowline_platform.registry import PluginRegistry

# ROOT_SURFACE — the composed daily-driver surface, served at the bare ``/mcp``
# (every other named surface ``X`` lives at ``/X/mcp``). It is DEFINED in
# `gateway` (the bottom of the import graph — discovery needs it for the
# manifest default and issue-#38 projection) and re-exported here, where
# `surface_route` and `config.surfaces()` (which always keeps it present)
# reference it — one constant, one assumption.
__all__ = [
    "ROOT_SURFACE",
    "surface_route",
    "build_surface_mounts",
    "mount_gateway",
    "gateway_lifespan",
]

# DNS-rebinding protection off on the streamable-HTTP transport: the gateway sits
# behind the platform trust gate (reached on the tailnet), matching the
# governance plugin's own surfaces. Can be tightened via env in a later
# increment if the platform is ever exposed beyond the tailnet.
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def surface_route(surface: str) -> str:
    """The platform route a named surface is served at: ROOT_SURFACE → ``/mcp``,
    any other ``X`` → ``/X/mcp``."""
    return "/mcp" if surface == ROOT_SURFACE else f"/{surface}/mcp"


class _SurfaceMount:
    """Holds a named surface's session manager + ASGI handler so the lifespan can
    enter its `run()` and the app can mount its `handle_request`."""

    def __init__(
        self,
        surface: str,
        registry: PluginRegistry,
        connector: UpstreamConnector,
        allowlist: frozenset[str] | None = None,
    ) -> None:
        self.surface = surface
        self.route = surface_route(surface)
        server = build_surface_server(registry, surface, connector, allowlist)
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
    surfaces: tuple[str, ...] | None = None,
) -> list[_SurfaceMount]:
    """One `_SurfaceMount` per named surface. `surfaces` defaults to the
    configured set (`config.surfaces()` ← ``SNOWLINE_SURFACES``); `connector`
    defaults to the production streamable-HTTP connector; tests inject an
    in-memory one.

    This is where `SNOWLINE_SURFACE_PLUGINS` is parsed + validated ONCE (issue
    #36 review): `config.surface_plugins()` fail-louds on malformed shape, then
    `config.validate_surface_plugins` rejects an allowlist naming a surface not
    in the mounted set (operators list a constrained surface in BOTH envs — the
    left-hand-typo guard). The per-surface allowlists are handed down FROZEN to
    each surface's gateway; `discover_upstreams` never re-reads the env, so a
    bad config is structurally a boot failure, never a mid-run surprise."""
    conn = connector or StreamableHttpConnector()
    names = config.surfaces() if surfaces is None else surfaces
    allowlists = config.surface_plugins()
    config.validate_surface_plugins(allowlists, tuple(names))
    return [
        _SurfaceMount(s, registry, conn, allowlists.get(s)) for s in names
    ]


def mount_gateway(app, mounts: list[_SurfaceMount]) -> None:
    """Mount each surface's ASGI handler on the FastAPI/Starlette `app`.

    Routes are added MOST-SPECIFIC first so Starlette's first-match routing
    picks the right surface; ``/mcp`` (the ROOT_SURFACE) is mounted last.
    Specificity is PREFIX specificity — number of path segments desc, then length
    desc — NOT raw `len(route)`: a route that is a path-prefix of another (e.g.
    ``/a/mcp`` is a prefix of ``/a/b/mcp``) must be mounted AFTER it so it can't
    shadow the deeper route, and segment count captures that where length does
    not. Mounting (vs a single route) lets the streamable-HTTP transport own the
    sub-path (GET/POST/DELETE + the session sub-routes it manages)."""
    ordered = sorted(
        mounts,
        key=lambda m: (m.route.count("/"), len(m.route)),
        reverse=True,
    )
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
