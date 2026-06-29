"""The Snowline platform app.

Wires the trust layer onto the platform surface and mounts the plugin registry.
`/health` is exempt (liveness); `/whoami` echoes the resolved principal; the
`/plugins` registry routes ride behind the trust gate. The gateway that proxies
registered plugins' MCP surfaces + UIs, and the health checker, build on top of
this registry.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request

from snowline_platform import config, plugins_routes, scopes_routes
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
    yield


def create_app(
    resolver: TrustResolver | None = None,
    registry: PluginRegistry | None = None,
    migrate_on_startup: bool = True,
) -> FastAPI:
    """Build the platform app. `resolver`/`registry` are injectable for tests;
    `migrate_on_startup=False` skips the lifespan boot-migrate (tests provision
    their own schema)."""
    app = FastAPI(title="Snowline Platform", lifespan=_lifespan)
    app.state.registry = registry or PluginRegistry()
    app.state.migrate_on_startup = migrate_on_startup
    app.add_middleware(
        TrustMiddleware,
        resolver=resolver or build_resolver(),
        exempt_paths={"/health"},
    )
    app.include_router(plugins_routes.router)
    app.include_router(scopes_routes.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/whoami")
    async def whoami(request: Request) -> dict:
        principal = request.state.principal
        return {"id": principal.id, "source": principal.source}

    return app


app = create_app()
