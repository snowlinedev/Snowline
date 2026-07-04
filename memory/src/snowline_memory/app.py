"""The memory plugin app.

A FastAPI app that:
  - serves the `main` MCP surface (the memory tools) over streamable HTTP at
    `/mcp`,
  - exposes `/health` (the platform supervisor polls this),
  - REGISTERS with the platform via a lifespan-long registration HEARTBEAT
    (issue #39): the manifest is POSTed at boot and re-asserted every interval,
    so a platform restart (in-memory registry, boots empty) self-heals. Each
    beat is best-effort, so a briefly-down platform doesn't crash the plugin
    (architecture §3, hot-pluggable).

Memory has its OWN database; like governance it boot-migrates to the latest
Alembic head in the lifespan (memory: server auto-migrates on boot), so a schema
change deploys on a plain restart. The FastMCP surface's session manager must be
entered for the app lifespan (the monolith pattern); registration happens after
the surface is up.
"""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

import anyio
from fastapi import FastAPI
from snowline_plugin_sdk.registration import install_heartbeat_httpx_filter
from snowline_plugin_sdk.replication import replication_delivery_loop
from snowline_plugin_sdk.replication.admin import build_replication_router

from snowline_memory import config, memory, registration
from snowline_memory.db import session_scope
from snowline_memory.mcp_surface import build_main_surface

# The manifest ingest path §4 declares (matched by the pairing CLI); memory's
# replication routes mount under this + the admin prefix, as plain FastAPI routes
# registered BEFORE the catch-all `/` MCP mount so Starlette matches them first.
REPLICATION_INGEST_PATH = "/events/ingest"


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
    replicate_on_startup: bool = True,
) -> FastAPI:
    """Build the memory app. `migrate_on_startup=False` skips the boot-migrate
    (tests provision their own schema); `register_on_startup=False` skips the
    platform registration heartbeat (tests assert registration separately);
    `replicate_on_startup=False` skips the replication DELIVERY loop (tests
    without a store, or that drive delivery by hand, turn it off — the ingest +
    admin ROUTES are always mounted; they touch the store only per request),
    forwarded straight through as the SDK loop's own `enabled` seam
    (issue #91)."""
    # Drop ONLY the heartbeat's per-beat `POST …/plugins` INFO line from the
    # httpx logger (idempotent; rationale lives with the filter in the SDK).
    install_heartbeat_httpx_filter()
    main_surface = build_main_surface()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        if getattr(app.state, "migrate_on_startup", True):
            _migrate_to_head()
        # The FastMCP surface owns a session manager that must be entered for the
        # app lifespan (the monolith/governance pattern).
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(main_surface.session_manager.run())
            # The registration HEARTBEAT (issue #39): first beat immediately
            # (the boot registration), then a re-assert every interval so a
            # platform restart — whose in-memory registry boots empty — heals
            # without this plugin being kickstarted too. Each beat is
            # best-effort and runs off the event loop; riding a task group
            # means boot never blocks on a slow/down platform (governance's
            # lifespan pattern).
            tg = await stack.enter_async_context(anyio.create_task_group())
            # The replication DELIVERY loop (replication-continuity §3, #80)
            # drains memory's transactional outbox — `memory.set` /
            # `memory.forgotten` events — to paired peers on a timer, mirroring
            # the registration heartbeat's best-effort task-group placement.
            # `replicate_on_startup` is forwarded straight through as the
            # SDK loop's own `enabled` defer/gate seam (issue #91) rather than
            # memory deciding locally whether to start the loop; it also still
            # no-ops with no subscriptions, so an unpaired instance just ticks
            # quietly.
            tg.start_soon(
                partial(
                    replication_delivery_loop,
                    session_scope,
                    enabled=getattr(app.state, "replicate_on_startup", True),
                )
            )
            if getattr(app.state, "register_on_startup", True):
                tg.start_soon(registration.registration_heartbeat)
            yield
            tg.cancel_scope.cancel()

    app = FastAPI(title="Snowline Memory", lifespan=_lifespan)
    app.state.migrate_on_startup = migrate_on_startup
    app.state.register_on_startup = register_on_startup
    app.state.replicate_on_startup = replicate_on_startup

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "plugin": registration.PLUGIN_NAME}

    # Memory's replication HTTP surface (replication-continuity §5, #80): the
    # signed-event ingest route + the tailnet-gated admin routes the pairing CLI
    # drives. Registered BEFORE the `/` MCP mount (same reason `/health` is) so
    # Starlette matches these routes ahead of the catch-all mount. `apply_event`
    # is memory's idempotent per-name-LWW domain apply.
    app.include_router(
        build_replication_router(
            session_scope,
            memory.apply_event,
            ingest_path=REPLICATION_INGEST_PATH,
        )
    )

    # Mount the FastMCP surface so the served endpoint is exactly the path the
    # manifest advertises: `/mcp`. FastMCP's `streamable_http_app()` ALREADY
    # serves at its own internal `streamable_http_path` (default `/mcp`), so we
    # mount at the PREFIX (`/`), NOT the full path — mounting at `/mcp` would
    # double to `/mcp/mcp` (the #28 lesson governance hit). `/health` is a route
    # registered above, so it still wins over the `/` mount.
    app.mount("/", main_surface.streamable_http_app())

    return app


app = create_app()
