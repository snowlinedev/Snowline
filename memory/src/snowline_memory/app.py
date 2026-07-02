"""The memory plugin app.

A FastAPI app that:
  - serves the `main` MCP surface (the memory tools) over streamable HTTP at
    `/mcp`,
  - exposes `/health` (the platform supervisor polls this),
  - REGISTERS with the platform on startup by POSTing its manifest to the
    platform's `POST /plugins` — best-effort and single-shot, so a briefly-down
    platform doesn't crash the plugin (architecture §3, hot-pluggable); a failed
    registration is only re-attempted by a restart (platform#39 owns the
    heartbeat/retry design).

Memory has its OWN database; like governance it boot-migrates to the latest
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

from snowline_memory import config, registration
from snowline_memory.mcp_surface import build_main_surface


def _migrate_to_head() -> None:
    """Bring the memory DB to the latest Alembic head — the boot-migrate
    governance/the monolith do in their lifespan. Reads the same DB URL the app's
    sessions use, so a schema change deploys on a plain restart."""
    from alembic import command
    from alembic.config import Config

    migrations = Path(__file__).resolve().parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations))
    cfg.set_main_option("sqlalchemy.url", config.database_url())
    command.upgrade(cfg, "head")


def create_app(
    *,
    migrate_on_startup: bool = True,
    register_on_startup: bool = True,
) -> FastAPI:
    """Build the memory app. `migrate_on_startup=False` skips the boot-migrate
    (tests provision their own schema); `register_on_startup=False` skips the
    platform registration POST (tests assert registration separately)."""
    main_surface = build_main_surface()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        if getattr(app.state, "migrate_on_startup", True):
            _migrate_to_head()
        # The FastMCP surface owns a session manager that must be entered for the
        # app lifespan (the monolith/governance pattern).
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(main_surface.session_manager.run())
            if getattr(app.state, "register_on_startup", True):
                # Single-shot, best-effort registration (never raises, so a down
                # platform can't crash boot; no retry loop — platform#39 owns the
                # heartbeat/retry design). The POST runs in a thread so it can't
                # block the event loop, but the lifespan AWAITS it before
                # yielding, so readiness (/health) waits up to the registration
                # timeout (~10s) when the platform is slow or unreachable.
                await anyio.to_thread.run_sync(registration.register_with_platform)
            yield

    app = FastAPI(title="Snowline Memory", lifespan=_lifespan)
    app.state.migrate_on_startup = migrate_on_startup
    app.state.register_on_startup = register_on_startup

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "plugin": registration.PLUGIN_NAME}

    # Mount the FastMCP surface so the served endpoint is exactly the path the
    # manifest advertises: `/mcp`. FastMCP's `streamable_http_app()` ALREADY
    # serves at its own internal `streamable_http_path` (default `/mcp`), so we
    # mount at the PREFIX (`/`), NOT the full path — mounting at `/mcp` would
    # double to `/mcp/mcp` (the #28 lesson governance hit). `/health` is a route
    # registered above, so it still wins over the `/` mount.
    app.mount("/", main_surface.streamable_http_app())

    return app


app = create_app()
