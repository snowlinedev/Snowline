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

Stateful surfaces + `tools/list_changed` (issue #240): each aggregated surface
runs a STATEFUL session manager so the server keeps each client's session and
its standalone GET stream, and can therefore PUSH `notifications/tools/
list_changed` when the registry changes the surface's upstream set (see
`gateway_notify`). The lifespan subscribes each surface's notifier to the
registry and runs its debounced broadcaster beside the session managers. The
platform's own tool app stays stateless — only the gateway dials it, per
request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
from mcp.server.lowlevel import Server
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
from snowline_platform.gateway_notify import (
    ListChangedServer,
    SessionSet,
    SurfaceChangeNotifier,
)
from snowline_platform.registry import PluginRegistry
from snowline_platform.registry_state import StartupGrace

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
    "build_platform_tools_mount",
    "mount_gateway",
    "gateway_lifespan",
]

# DNS-rebinding protection off on the streamable-HTTP transport: the gateway sits
# behind the platform trust gate (reached on the tailnet or loopback, per
# governance decision 35546152), matching the governance plugin's own surfaces.
# Public exposure never widens this CIDR gate to cover it (Snowline#120's
# OAuth-terminating edge front authenticates instead) — so there is no "tighten
# the CIDR set" increment triggered by exposure; only a platform-served public
# path would need a new (bearer-token) TrustProvider alongside this one.
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def surface_route(surface: str) -> str:
    """The platform route a named surface is served at: ROOT_SURFACE → ``/mcp``,
    any other ``X`` → ``/X/mcp``."""
    return "/mcp" if surface == ROOT_SURFACE else f"/{surface}/mcp"


class _ServerMount:
    """One low-level MCP `Server` served over streamable-HTTP at `route`: holds
    the session manager + ASGI handler so the lifespan can enter its `run()` and
    the app can mount its `handle_request`.

    The shared base for an aggregated gateway surface (`_SurfaceMount`) AND the
    platform's OWN tool app (`build_platform_tools_mount`, decision 0503fff0) —
    both serve a low-level `Server` over the same streamable-HTTP machinery, so
    the mount/lifespan wiring is written once here rather than duplicated. The
    platform tool app is served EXACTLY like a composed surface; the only thing
    that makes it "the platform's own upstream" is the registry self-entry that
    the gateway then dials back over loopback."""

    def __init__(
        self,
        route: str,
        server: Server,
        *,
        stateless: bool = True,
        session_idle_timeout: float | None = None,
    ) -> None:
        self.route = route
        # Stateless by default: the platform tool app (each tool opens a fresh
        # `session_scope()`) holds no per-session server state and is only ever
        # dialed per-request by the gateway, so a stateless transport is the
        # honest model there. Aggregated SURFACES opt into stateful
        # (`_SurfaceMount`, issue #240): not because the gateway holds tool
        # state — list/call still re-discover upstreams and open a fresh
        # upstream session per request — but because only a live session with
        # a GET stream can receive a server-pushed `tools/list_changed`. The
        # cost stateless avoided is session affinity: the session lives in this
        # process's memory, which is fine for the single-process platform (one
        # uvicorn worker; the remote front forwards `Mcp-Session-Id`). A
        # platform restart drops every session; clients get 404 on their old
        # id and re-initialize, per the MCP spec.
        self._manager = StreamableHTTPSessionManager(
            app=server,
            stateless=stateless,
            security_settings=_SECURITY,
            session_idle_timeout=None if stateless else session_idle_timeout,
        )

    async def asgi(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._manager.handle_request(scope, receive, send)

    def run(self):
        return self._manager.run()


class _SurfaceMount(_ServerMount):
    """A `_ServerMount` for one NAMED platform surface: the low-level server is
    the gateway aggregator (`build_surface_server`) for that surface, served
    STATEFUL, with a `SurfaceChangeNotifier` that pushes `tools/list_changed` to
    its live sessions when the registry changes its upstream set (issue #240),
    and an optional `StartupGrace` awaited before each `tools/list`."""

    def __init__(
        self,
        surface: str,
        registry: PluginRegistry,
        connector: UpstreamConnector,
        allowlist: frozenset[str] | None = None,
        grace: StartupGrace | None = None,
    ) -> None:
        self.surface = surface
        self.registry = registry
        self.sessions = SessionSet()
        server = ListChangedServer(f"snowline-{surface}", self.sessions)
        build_surface_server(
            registry,
            surface,
            connector,
            allowlist,
            server=server,
            before_list=grace.wait if grace is not None else None,
        )
        self.notifier = SurfaceChangeNotifier(
            registry, surface, self.sessions, allowlist
        )
        super().__init__(
            surface_route(surface),
            server,
            stateless=False,
            session_idle_timeout=config.gateway_session_idle_timeout(),
        )


def build_surface_mounts(
    registry: PluginRegistry,
    connector: UpstreamConnector | None = None,
    surfaces: tuple[str, ...] | None = None,
    grace: StartupGrace | None = None,
) -> list[_SurfaceMount]:
    """One `_SurfaceMount` per named surface. `surfaces` defaults to the
    configured set (`config.surfaces()` ← ``SNOWLINE_SURFACES``); `connector`
    defaults to the production streamable-HTTP connector; tests inject an
    in-memory one. `grace` (issue #240) is the shared post-restart
    `StartupGrace` every surface's `tools/list` awaits; None = no grace.

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
        _SurfaceMount(s, registry, conn, allowlists.get(s), grace) for s in names
    ]


def build_platform_tools_mount() -> _ServerMount:
    """The SERVE half of the platform-as-its-own-upstream (decision 0503fff0): the
    platform's native scope/milestone tool app (`platform_tools.
    build_platform_tools_surface`) as a `_ServerMount` at `/platform/mcp`, using
    the SAME streamable-HTTP machinery as an aggregated surface.

    The COMPOSE half is the registry self-entry (`platform_tools.
    platform_self_manifest`), seeded at app startup: the gateway dials THIS app
    back over the platform's own loopback base_url and aggregates it onto `main`
    like any plugin. Imported lazily to keep `gateway_app` free of a
    `platform_tools` → services import at module load (and to avoid any import
    cycle)."""
    from snowline_platform.platform_tools import (
        PLATFORM_MCP_PATH,
        build_platform_tools_surface,
    )

    # FastMCP wraps a low-level `Server` (`._mcp_server`) — the same server type
    # `build_surface_server` returns — so it drops straight into `_ServerMount`.
    surface = build_platform_tools_surface()
    return _ServerMount(PLATFORM_MCP_PATH, surface._mcp_server)


def mount_gateway(app, mounts: list[_ServerMount]) -> None:
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
    mounts: list[_ServerMount],
) -> AsyncIterator[None]:
    """Enter every surface session manager's `run()` for the app lifespan, and
    — for each aggregated surface — subscribe its change notifier to the
    registry and run its debounced broadcaster (issue #240). Exit order is the
    reverse: broadcasters cancelled and unsubscribed first, then the session
    managers torn down."""
    async with AsyncExitStack() as stack:
        for mount in mounts:
            await stack.enter_async_context(mount.run())
        surface_mounts = [m for m in mounts if isinstance(m, _SurfaceMount)]
        tg = await stack.enter_async_context(anyio.create_task_group())
        stack.callback(tg.cancel_scope.cancel)
        for mount in surface_mounts:
            tg.start_soon(mount.notifier.run)
            mount.registry.add_observer(mount.notifier.registry_changed)
            stack.callback(
                mount.registry.remove_observer, mount.notifier.registry_changed
            )
        yield
