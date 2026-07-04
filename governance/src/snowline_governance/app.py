"""The governance plugin app.

A FastAPI app that:
  - serves the `main` MCP surface (the decision tools) over streamable HTTP at
    `/mcp`,
  - exposes `/health` (the platform supervisor polls this),
  - REGISTERS with the platform via a lifespan-long registration HEARTBEAT
    (issue #39): the manifest is POSTed at boot and re-asserted every interval,
    so a platform restart (in-memory registry, boots empty) self-heals. Each
    beat is best-effort, so a briefly-down platform doesn't crash the plugin
    (architecture §3, hot-pluggable).

Governance has its OWN database; like the platform it boot-migrates to the latest
Alembic head in the lifespan (memory: server auto-migrates on boot), so a schema
change deploys on a plain restart. The FastMCP surface's session manager must be
entered for the app lifespan (the monolith pattern); registration happens after
the surface is up.
"""

from __future__ import annotations

import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI
from snowline_plugin_sdk.registration import HeartbeatHttpxLogFilter

from snowline_governance import config, registration
from snowline_governance.mcp_surface import build_main_surface, build_shadow_surface
from snowline_governance.scope_client import ScopeClient
from snowline_governance.ui_api import router as ui_api_router

# Drops httpx's per-request INFO line for the registration heartbeat's
# `POST …/plugins` (one line per beat, forever) while letting every OTHER httpx
# request trace through — governance also talks httpx for scope reads and the
# webhook_delivery_loop's outbound deliveries, and muting those (a process-wide
# WARNING cap) would leave live debugging blind. The filter now lives in the SDK
# (issue #50), shared with memory + the other plugins.
_HEARTBEAT_HTTPX_FILTER = HeartbeatHttpxLogFilter()


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
    schema); `register_on_startup=False` skips the platform registration
    heartbeat (tests assert registration separately, against a stubbed
    platform)."""
    # httpx logs every request at INFO — with the registration heartbeat that is
    # one line per beat forever (defeating the DEBUG steady-state logging).
    # Capping the whole httpx logger at WARNING would also mute the scope reads
    # and webhook_delivery_loop's httpx traffic, so instead drop ONLY the
    # heartbeat's POST /plugins lines (idempotent — one module-level filter
    # instance, and addFilter dedupes).
    logging.getLogger("httpx").addFilter(_HEARTBEAT_HTTPX_FILTER)
    main_surface = build_main_surface(scope_client=scope_client)
    shadow_surface = build_shadow_surface(scope_client=scope_client)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        if getattr(app.state, "migrate_on_startup", True):
            _migrate_to_head()
        # Each FastMCP surface owns a session manager that must be entered for the
        # app lifespan (the monolith pattern). Both the `main` and `shadow`
        # surfaces run for the whole app lifespan.
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(main_surface.session_manager.run())
            await stack.enter_async_context(shadow_surface.session_manager.run())
            # The decision-event EMIT delivery loop (spec §7) rides the lifespan
            # in a task group, mirroring how the monolith runs its delivery /
            # reconcile loops. It drains the transactional-outbox deliveries on a
            # timer and signs+POSTs them; disable via SNOWLINE_WEBHOOK_DISABLED.
            # The scope is cancelled on exit so the loop tears down cleanly.
            from snowline_governance.replication import webhook_delivery_loop

            tg = await stack.enter_async_context(anyio.create_task_group())
            tg.start_soon(webhook_delivery_loop)
            if getattr(app.state, "register_on_startup", True):
                # The registration HEARTBEAT (issue #39): first beat immediately
                # (the boot registration), then a re-assert every interval so a
                # platform restart — whose in-memory registry boots empty — heals
                # without this plugin being kickstarted too. Each beat is
                # best-effort and runs off the event loop; riding the task group
                # means boot no longer blocks on a slow/down platform at all.
                tg.start_soon(registration.registration_heartbeat)
            yield
            tg.cancel_scope.cancel()

    app = FastAPI(title="Snowline Governance", lifespan=_lifespan)
    app.state.migrate_on_startup = migrate_on_startup
    app.state.register_on_startup = register_on_startup

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "plugin": registration.PLUGIN_NAME}

    # The `/ui-api` read routes (ui-shell.md §3/§5, issue #55) — the platform's
    # `/ui-api/<plugin>/...` proxy forwards here. Registered as a plain FastAPI
    # route BEFORE the catch-all MCP mounts below, same reason `/health` wins
    # over the `/` mount: Starlette matches routes before mounts in registration
    # order, and a mount at `/` would otherwise swallow every path.
    app.include_router(ui_api_router)

    # Mount the FastMCP surfaces so the served endpoints are exactly the paths the
    # manifest advertises: `/mcp` (main) and `/shadow/mcp` (shadow). FastMCP's
    # `streamable_http_app()` ALREADY serves at its own internal
    # `streamable_http_path` (default `/mcp`), so we mount at the PREFIX, not the
    # full path — the monolith pattern (`app.mount("/", root)`, `app.mount("/core",
    # core)`). Mounting main at `/mcp` would double to `/mcp/mcp`; instead main →
    # `/` (serves `/mcp`) and shadow → `/shadow` (serves `/shadow/mcp`). The shadow
    # surface is a SEPARATE FastMCP instance, so the real-write verbs it omits are
    # physically unreachable from a speculation session (decision 8a7f0a11).
    # More-specific `/shadow` is mounted BEFORE `/` so Starlette matches it first;
    # `/health` is a route registered above, so it still wins over the `/` mount.
    app.mount("/shadow", shadow_surface.streamable_http_app())
    app.mount("/", main_surface.streamable_http_app())

    return app


app = create_app()
