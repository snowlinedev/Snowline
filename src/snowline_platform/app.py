"""The Snowline platform app.

Wires the trust layer onto the platform surface and mounts the plugin registry.
`/health` is exempt (liveness); `/whoami` echoes the resolved principal; the
`/plugins` registry routes ride behind the trust gate. The gateway that proxies
registered plugins' MCP surfaces + UIs, and the health checker, build on top of
this registry.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

import anyio
from fastapi import FastAPI, Request

from snowline_platform import config, plugins_routes, scopes_routes
from snowline_platform.gateway import UpstreamConnector
from snowline_platform.gateway_app import (
    build_surface_mounts,
    gateway_lifespan,
    mount_gateway,
)
from snowline_platform.health import health_poll_loop
from snowline_platform.middleware import TrustMiddleware
from snowline_platform.registry import PluginRegistry
from snowline_platform.trust import CidrTrustProvider, TrustResolver


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
    if getattr(app.state, "migrate_on_startup", True):
        _migrate_to_head()
    # Enter every aggregated MCP surface's streamable-HTTP session manager for the
    # app lifespan (the gateway, gateway.md §2). The surfaces are mounted at
    # create_app time; their managers' run() is the required-for-lifespan context.
    async with gateway_lifespan(app.state.gateway_mounts):
        # The health poller (health.md): a background task that marks each plugin
        # UP/DOWN so the gateway routes around dead ones. Off by default (the
        # test-friendly factory) — the production singleton opts in. Cancelled on
        # shutdown by tearing down its task group.
        if getattr(app.state, "poll_health", False):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    partial(
                        health_poll_loop,
                        app.state.registry,
                        interval=config.health_poll_interval(),
                        timeout=config.health_poll_timeout(),
                    )
                )
                try:
                    yield
                finally:
                    # Cancel the poller even if serving raised; the task group
                    # absorbs its own-scope cancellation, so shutdown stays clean.
                    tg.cancel_scope.cancel()
        else:
            yield


def create_app(
    resolver: TrustResolver | None = None,
    registry: PluginRegistry | None = None,
    migrate_on_startup: bool = True,
    connector: UpstreamConnector | None = None,
    poll_health: bool = False,
) -> FastAPI:
    """Build the platform app. `resolver`/`registry` are injectable for tests;
    `migrate_on_startup=False` skips the lifespan boot-migrate (tests provision
    their own schema); `connector` injects the gateway's upstream connector
    (defaults to streamable-HTTP; tests pass an in-memory one). `poll_health`
    starts the background health poller — OFF by default so unit tests don't spawn
    network traffic or race on status; the production singleton opts in."""
    app = FastAPI(title="Snowline Platform", lifespan=_lifespan)
    app.state.registry = registry or PluginRegistry()
    app.state.migrate_on_startup = migrate_on_startup
    app.state.poll_health = poll_health
    app.add_middleware(
        TrustMiddleware,
        resolver=resolver or build_resolver(),
        exempt_paths={"/health"},
    )
    app.include_router(plugins_routes.router)
    app.include_router(scopes_routes.router)

    # The gateway: aggregate registered plugins' MCP surfaces onto the platform's
    # named surfaces and mount each as a streamable-HTTP endpoint (e.g. /mcp,
    # /shadow/mcp), behind the trust gate. Mounts share the app's registry, so a
    # plugin registered at runtime is composed without a restart.
    mounts = build_surface_mounts(app.state.registry, connector=connector)
    app.state.gateway_mounts = mounts
    mount_gateway(app, mounts)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/whoami")
    async def whoami(request: Request) -> dict:
        principal = request.state.principal
        return {"id": principal.id, "source": principal.source}

    return app


# The production singleton: boot-migrate, gateway, AND the health poller on.
app = create_app(poll_health=True)
