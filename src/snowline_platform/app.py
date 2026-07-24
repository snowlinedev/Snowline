"""The Snowline platform app.

Wires the trust layer onto the platform surface and mounts the plugin registry.
`/health` is exempt (liveness); `/whoami` echoes the resolved principal; the
`/plugins` registry routes ride behind the trust gate. The gateway that proxies
registered plugins' MCP surfaces + UIs, and the health checker, build on top of
this registry.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

import anyio
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse

from snowline_platform import (
    config,
    milestones_routes,
    platform_tools,
    plugins_routes,
    replication,
    scopes_routes,
    surfaces_routes,
    ui_api,
)
from snowline_platform.db import session_scope
from snowline_platform.gateway import UpstreamConnector
from snowline_platform.gateway_app import (
    build_platform_tools_mount,
    build_surface_mounts,
    gateway_lifespan,
    mount_gateway,
)
from snowline_platform.health import health_poll_loop
from snowline_platform.middleware import TrustMiddleware
from snowline_platform.registry import PluginRegistry
from snowline_platform.trust import CidrTrustProvider, TrustResolver
from snowline_plugin_sdk.replication import replication_delivery_loop

log = logging.getLogger("snowline_platform.app")


def build_resolver() -> TrustResolver:
    # v1: one provider — the configurable trusted-CIDR network gate. An OAuth
    # provider would be PREPENDED here later (token-first, CIDR fallback).
    return TrustResolver([CidrTrustProvider(config.trusted_cidrs())])


def _migrate_to_head() -> None:
    """Bring the platform DB to the latest Alembic head — the boot-migrate the
    monolith does in its lifespan (memory: server auto-migrates on boot). Reads
    the same DB URL the app's sessions use, so a schema change deploys on a plain
    restart."""
    from alembic import command
    from alembic.config import Config

    migrations = Path(__file__).resolve().parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations))
    cfg.set_main_option("sqlalchemy.url", config.database_url())
    command.upgrade(cfg, "head")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        if getattr(app.state, "migrate_on_startup", True):
            _migrate_to_head()
        # Enter every aggregated MCP surface's streamable-HTTP session manager for
        # the app lifespan (the gateway, gateway.md §2). The surfaces are mounted
        # at create_app time; their managers' run() is the required-for-lifespan
        # context.
        async with gateway_lifespan(app.state.gateway_mounts):
            # The registry is in-memory, so a restart boots it with ONLY the
            # platform self-entry (seeded in create_app, decision 0503fff0) and
            # every mounted surface serves only the platform's native tools until
            # the plugins' registration heartbeats re-upsert them (issue #39).
            # That window is expected — but it must be LOUD, not silent: a
            # crash-restart under launchd at 3am otherwise looks healthy while the
            # gateway is hollow of PLUGIN tools. The platform self-entry is not a
            # plugin heartbeat, so it is excluded from the count — otherwise the
            # always-present self-entry would permanently silence this signal.
            external = [
                e
                for e in app.state.registry.list()
                if e.manifest.name != platform_tools.PLATFORM_PLUGIN_NAME
            ]
            if app.state.gateway_mounts and not external:
                log.warning(
                    "boot: %d gateway surface(s) mounted but ZERO plugins "
                    "registered — composed surfaces serve only the platform's own "
                    "native tools until plugin registration heartbeats arrive "
                    "(issue #39)",
                    len(app.state.gateway_mounts),
                )
            # The health poller (health.md) and the replication delivery loop
            # (spec §8/§9 item 5, issue #81) are both OFF by default (the
            # test-friendly factory) — the production singleton opts into
            # both. Same task group either way so a single cancel_scope tears
            # down whichever is running on shutdown. The replication loop is
            # gated via its own `enabled` seam (issue #91), not by whether
            # it's started at all — see below.
            poll_health = getattr(app.state, "poll_health", False)
            replicate = getattr(app.state, "replicate", False)
            if poll_health or replicate:
                async with anyio.create_task_group() as tg:
                    if poll_health:
                        tg.start_soon(
                            partial(
                                health_poll_loop,
                                app.state.registry,
                                interval=config.health_poll_interval(),
                                timeout=config.health_poll_timeout(),
                            )
                        )
                    # Drains the platform's OWN scope.created/scope.updated
                    # outbox on a timer, same as any opted-in plugin (§3.1) —
                    # harmless with zero subscriptions (the common case
                    # before pairing, #82, lands). Always started; `enabled`
                    # is the SDK's own defer/gate seam (issue #91), so
                    # `replicate=False` (the test-friendly default) makes
                    # this an immediate no-op instead of the platform
                    # hand-rolling the gate around whether to start it.
                    tg.start_soon(
                        partial(
                            replication_delivery_loop, session_scope, enabled=replicate
                        )
                    )
                    try:
                        yield
                    finally:
                        # Cancel even if serving raised; the task group
                        # absorbs its own-scope cancellation, so shutdown
                        # stays clean.
                        tg.cancel_scope.cancel()
            else:
                yield
    finally:
        # The /ui-api proxy's shared httpx client (ui_api.py) is created
        # lazily on first request, not here — but however it exits, close it
        # if one was ever created, so a restart doesn't leak an open client.
        await ui_api.aclose_client(app)


def create_app(
    resolver: TrustResolver | None = None,
    registry: PluginRegistry | None = None,
    migrate_on_startup: bool = True,
    connector: UpstreamConnector | None = None,
    poll_health: bool = False,
    replicate: bool = False,
) -> FastAPI:
    """Build the platform app. `resolver`/`registry` are injectable for tests;
    `migrate_on_startup=False` skips the lifespan boot-migrate (tests provision
    their own schema); `connector` injects the gateway's upstream connector
    (defaults to streamable-HTTP; tests pass an in-memory one). `poll_health`
    starts the background health poller and `replicate` gates the replication
    delivery loop (spec §8, issue #81) — both OFF by default so unit tests
    don't spawn network traffic or race on status; the production singleton
    opts into both. `replicate` is forwarded straight through as the SDK
    loop's own `enabled` seam (issue #91) rather than deciding locally whether
    to start the loop at all."""
    app = FastAPI(title="Snowline Platform", lifespan=_lifespan)
    app.state.registry = registry or PluginRegistry()
    # Seed the platform's OWN upstream (decision 0503fff0): a `platform` registry
    # entry at the platform's loopback base_url mapping `/platform/mcp → main`, so
    # the gateway composes the native scope/milestone tools onto `main` through
    # the ORDINARY aggregation path — no special-casing in the aggregator, the
    # tools surface `platform__<tool>` like any plugin's. Seeded HERE at startup
    # (the same way the replication self-manifest is platform-owned, §8) rather
    # than via an external registration call, so it needs no bootstrap client and
    # survives a restart: the in-memory registry boots empty and — unlike a plugin
    # that re-upserts on its heartbeat — the platform re-seeds itself every boot.
    # `upsert` is idempotent, so an injected registry that already holds it is a
    # no-op. It is a plain registry entry, so it also appears in GET /plugins and
    # is health-checked against its own loopback /health like any plugin (no
    # exemption — the platform's /health answers 200 while it is serving).
    app.state.registry.upsert(platform_tools.platform_self_manifest())
    app.state.migrate_on_startup = migrate_on_startup
    app.state.poll_health = poll_health
    app.state.replicate = replicate
    app.add_middleware(
        TrustMiddleware,
        resolver=resolver or build_resolver(),
        exempt_paths={"/health"},
    )
    app.include_router(plugins_routes.router)
    app.include_router(scopes_routes.router)
    # The milestone registry read/resolve + create/lifecycle surface
    # (milestones.md §5), the platform's SECOND identity primitive beside
    # scopes. Behind the trust gate like every other platform route.
    app.include_router(milestones_routes.router)
    # The replication ingest + admin surface (spec §5, §8): the platform's own
    # opted-in stream, identical shape to what a plugin mounts. Rides behind
    # the trust middleware like every other route — the router's own tailnet
    # CIDR check (`_require_trusted`) is defense in depth, not a substitute.
    app.include_router(replication.router)
    app.include_router(surfaces_routes.router)
    app.include_router(ui_api.router)

    # The dashboard owns the /ui and /ui-api route namespaces (ui-shell.md
    # §5–§6), and `platform` is the platform's OWN tool-app route (/platform/mcp,
    # decision 0503fff0); a gateway surface named after any of them would mount
    # /<name>/mcp on top of those and silently interleave MCP transports or SPA
    # assets — fail loud at boot like every other surface-config error.
    reserved = {"ui", "ui-api", platform_tools.PLATFORM_PLUGIN_NAME} & set(
        config.surfaces()
    )
    if reserved:
        raise config.ConfigError(
            f"SNOWLINE_SURFACES uses reserved name(s) {sorted(reserved)!r} — "
            f"'ui'/'ui-api' are the dashboard's route namespaces and 'platform' "
            f"is the platform's own tool-app route (/platform/mcp)"
        )

    # The gateway: aggregate registered plugins' MCP surfaces onto the platform's
    # named surfaces and mount each as a streamable-HTTP endpoint (e.g. /mcp,
    # /shadow/mcp), behind the trust gate. Mounts share the app's registry, so a
    # plugin registered at runtime is composed without a restart.
    mounts = build_surface_mounts(app.state.registry, connector=connector)
    # The SERVE half of the platform-as-its-own-upstream: mount the native tool
    # app at /platform/mcp alongside the composed surfaces, entered in the same
    # gateway lifespan. The COMPOSE half is the self-entry seeded above; the
    # gateway dials this route back over loopback like any upstream. Kept OUT of
    # `build_surface_mounts` (which is per-named-surface, and asserted route-exact
    # by tests) — it is not a surface, it is a plugin-shaped tool endpoint.
    mounts.append(build_platform_tools_mount())
    app.state.gateway_mounts = mounts
    mount_gateway(app, mounts)

    # Freeze the surface config the moment the mounts are built: GET /surfaces
    # reports THIS, not a per-request env re-parse — the view can't drift from
    # what the gateway actually mounted, and a malformed env mutated after
    # boot can't turn the route into a 500 (config errors stay a boot-time
    # concern, per the fail-loud rule).
    allowlists = config.surface_plugins()
    app.state.surface_listing = [
        (name, allowlists.get(name)) for name in config.surfaces()
    ]

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/whoami")
    async def whoami(request: Request) -> dict:
        principal = request.state.principal
        return {"id": principal.id, "source": principal.source}

    # The dashboard shell (ui-shell.md §6): the built static bundle served at
    # /ui, with an SPA fallback so deep links (/ui/plugins, /ui/scopes, and
    # later /ui/<plugin>/<route>) render index.html and the client router takes
    # over. Served at /ui — NOT / — so shell routes can mirror API names
    # without colliding with the JSON routes (/plugins vs /ui/plugins), and to
    # pair with the phase-2 /ui-api data proxy. The dist dir is resolved
    # PER-REQUEST, so a bundle built after the platform booted starts serving
    # without a restart (first-deploy ordering), and environments with no
    # bundle 404 cleanly.
    # Cache policy (/ui): with NO Cache-Control, browsers apply HEURISTIC
    # freshness — mobile Safari serves a stale index.html referencing a
    # bundle that no longer exists, which reads as "the deploy didn't take"
    # on phones. So: everything revalidates every load (no-cache; the etag
    # makes that a cheap 304) EXCEPT files whose NAME carries vite's content
    # hash, which may cache forever. `private` (not `public`): /ui sits
    # behind the trust gate, and a shared cache on the path must not serve
    # gated content to peers the gate would 403 (decision 35546152 posture).
    # 404s carry no-cache too — 404 is heuristically cacheable (RFC 9110
    # §15.5.5), and a cached "no bundle yet" would defeat the per-request
    # dist resolution below.
    _NO_CACHE = {"Cache-Control": "no-cache"}
    _IMMUTABLE = {"Cache-Control": "private, max-age=31536000, immutable"}
    # vite's default assetFileNames: <name>-<hash>.<ext>. Matching the NAME,
    # not the assets/ directory, keeps verbatim-copied public/ files (which
    # keep their stable names wherever they land) on the revalidate path.
    _HASHED_NAME = re.compile(r"-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")

    @app.get("/ui")
    @app.get("/ui/{rest:path}")
    async def ui(rest: str = "") -> Response:
        dist = config.dashboard_dist()
        if dist is None:
            return JSONResponse(
                {"detail": "no dashboard bundle built"},
                status_code=status.HTTP_404_NOT_FOUND,
                headers=_NO_CACHE,
            )
        dist_dir = Path(dist)
        try:
            candidate = (dist_dir / rest).resolve()
        except (ValueError, OSError):
            # e.g. an embedded NUL from a percent-encoded scanner URL — treat
            # like any other non-file path and fall through to the shell.
            candidate = None
        # Traversal guard: only files inside the dist dir are servable;
        # anything else (including client-side routes) gets the SPA shell.
        if (
            rest
            and candidate is not None
            and candidate.is_file()
            and candidate.is_relative_to(dist_dir.resolve())
        ):
            immutable = bool(_HASHED_NAME.search(candidate.name))
            return FileResponse(
                candidate, headers=_IMMUTABLE if immutable else _NO_CACHE
            )
        # A miss under assets/ is a dead bundle reference (a shell cached
        # before a redeploy asking for a hash that no longer exists) — it
        # must 404, NOT SPA-fallback: serving index.html as a .js module
        # trips the browser's MIME check and renders a blank dashboard with
        # only a console error to explain it.
        if rest.startswith("assets/"):
            return JSONResponse(
                {"detail": "no such asset (stale shell? reload the page)"},
                status_code=status.HTTP_404_NOT_FOUND,
                headers=_NO_CACHE,
            )
        index = dist_dir / "index.html"
        if not index.is_file():
            # A half-built dist (vite mid-rebuild, interrupted build) must
            # read as "bundle missing", never as a 500 on every request.
            return JSONResponse(
                {"detail": "dashboard bundle incomplete — rebuild (npm run build)"},
                status_code=status.HTTP_404_NOT_FOUND,
                headers=_NO_CACHE,
            )
        return FileResponse(index, headers=_NO_CACHE)

    return app


# The production singleton: boot-migrate, gateway, health poller, AND the
# replication delivery loop on.
app = create_app(poll_health=True, replicate=True)
