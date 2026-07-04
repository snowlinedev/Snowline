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
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI
from snowline_plugin_sdk.registration import install_heartbeat_httpx_filter
from snowline_plugin_sdk.replication.admin import build_replication_router

from snowline_governance import config, registration, replication_apply
from snowline_governance.db import session_scope
from snowline_governance.mcp_surface import build_main_surface, build_shadow_surface
from snowline_governance.replication_stream import INGEST_PATH
from snowline_governance.scope_client import ScopeClient
from snowline_governance.ui_api import router as ui_api_router


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
    # Drop ONLY the heartbeat's per-beat `POST …/plugins` INFO line from the
    # httpx logger — governance's scope reads and webhook deliveries still
    # trace through (idempotent; rationale lives with the filter in the SDK).
    install_heartbeat_httpx_filter()
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
            from snowline_governance.turns import shadow_turn_loop
            from snowline_plugin_sdk.replication.emit import (
                replication_delivery_loop,
            )

            tg = await stack.enter_async_context(anyio.create_task_group())
            tg.start_soon(webhook_delivery_loop)
            # The replication-class delivery loop (replication-continuity
            # §3.1, #79): drains the SDK outbox toward paired peers with the
            # unbounded-retry/capped-backoff class. A no-op ticker until
            # pairing creates subscriptions; disable via
            # SNOWLINE_REPLICATION_DISABLED.
            tg.start_soon(replication_delivery_loop, session_scope)
            # The shadow turn-runner (spec §6, issue #71) rides the same task
            # group. It self-gates OFF unless SNOWLINE_SHADOW_TURNS_ENABLED is
            # set (default false → returns immediately; the tests also pin the
            # var off via an autouse fixture, so a dev shell's export can't
            # start real codex turns mid-suite). An injected ScopeClient is
            # passed through for tests; in production (scope_client=None) the
            # loop builds its OWN HttpScopeClient from the same config the
            # surfaces use — same platform URL, separate client instance.
            # Cancellation on lifespan exit abandons an in-flight turn thread
            # (it finishes in the background; see shadow_turn_loop).
            tg.start_soon(shadow_turn_loop, scope_client)
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

    # The replication ingest + admin surface (replication-continuity §4/§5,
    # #79): the SDK router over governance's OWN session_scope (one delivery
    # per request transaction — the ingest contract) and its domain apply.
    # Registered BEFORE the catch-all MCP mounts for the same
    # routes-before-mounts reason as /health and /ui-api. Tailnet-gated inside
    # the router; the stream HMAC authenticates each delivery. Admin stays OFF
    # MCP — agents never manage plumbing.
    app.include_router(
        build_replication_router(
            session_scope,
            replication_apply.build_apply(scope_client),
            ingest_path=INGEST_PATH,
        )
    )

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
