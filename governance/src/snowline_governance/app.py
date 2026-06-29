"""The governance plugin app.

A FastAPI app that:
  - serves the `main` MCP surface (the decision tools) over streamable HTTP at
    `/mcp`,
  - exposes `/health` (the platform supervisor polls this),
  - REGISTERS with the platform on startup by POSTing its manifest to the
    platform's `POST /plugins` — best-effort/retryable, so a briefly-down
    platform doesn't crash the plugin (architecture §3, hot-pluggable).

Governance has its OWN database; like the platform it boot-migrates to the latest
Alembic head in the lifespan (memory: server auto-migrates on boot), so a schema
change deploys on a plain restart. The FastMCP surface's session manager must be
entered for the app lifespan (the monolith pattern); registration happens after
the surface is up.
"""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI

from snowline_governance import config, registration
from snowline_governance.mcp_surface import build_main_surface
from snowline_governance.scope_client import ScopeClient


def _migrate_to_head() -> None:
    """Bring the governance DB to the latest Alembic head — the boot-migrate the
    platform/monolith do in their lifespan. Reads the same DB URL the app's
    sessions use, so a schema change deploys on a plain restart."""
    from alembic import command
    from alembic.config import Config

    migrations = Path(__file__).resolve().parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations))
    cfg.set_main_option("sqlalchemy.url", config.database_url())
    command.upgrade(cfg, "head")


def create_app(
    scope_client: ScopeClient | None = None,
    *,
    migrate_on_startup: bool = True,
    register_on_startup: bool = True,
) -> FastAPI:
    """Build the governance app. `scope_client` is injectable (tests pass a stub);
    `migrate_on_startup=False` skips the boot-migrate (tests provision their own
    schema); `register_on_startup=False` skips the platform registration POST
    (tests assert registration separately, against a stubbed platform)."""
    main_surface = build_main_surface(scope_client=scope_client)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        if getattr(app.state, "migrate_on_startup", True):
            _migrate_to_head()
        # The FastMCP surface owns a session manager that must be entered for the
        # app lifespan (the monolith pattern).
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(main_surface.session_manager.run())
            if getattr(app.state, "register_on_startup", True):
                # Best-effort (never raises, so a down platform can't crash boot)
                # AND off the event loop (the POST is blocking httpx with a
                # timeout — a slow-but-reachable platform must not stall startup /
                # delay /health coming up).
                await anyio.to_thread.run_sync(registration.register_with_platform)
            yield

    app = FastAPI(title="Snowline Governance", lifespan=_lifespan)
    app.state.migrate_on_startup = migrate_on_startup
    app.state.register_on_startup = register_on_startup

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "plugin": registration.PLUGIN_NAME}

    # Mount the `main` MCP surface (streamable HTTP) at /mcp — the path the
    # manifest maps onto the platform's `main` named-surface.
    app.mount("/mcp", main_surface.streamable_http_app())

    return app


app = create_app()
