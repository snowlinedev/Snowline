"""The plugin's MCP surface served over the REAL streamable-HTTP transport — a
true `ClientSession` connecting to the MOUNTED app at `/mcp` exactly as the
platform gateway does (driven in-process via httpx ASGITransport, but the full
transport stack runs).

This pins the served endpoint to the manifest path (`/mcp`, NOT `/mcp/mcp`) — the
#28 lesson governance encountered: `streamable_http_app()` already serves at its
own internal `/mcp`, so the surface must be mounted at the PREFIX (`/`).

No database needed: `list_tools` touches no store, migration + registration are
off — so the lifespan only starts the surface's session manager.
"""

from __future__ import annotations

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from snowline_memory.app import create_app


def _app():
    return create_app(migrate_on_startup=False, register_on_startup=False)


def _http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://mem",
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    )


async def _list_tools(app, route: str) -> list[str]:
    async with app.router.lifespan_context(app):
        async with _http(app) as http:
            async with streamable_http_client(
                f"http://mem{route}", http_client=http
            ) as (read, write, _sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return sorted(t.name for t in (await session.list_tools()).tools)


def test_main_surface_served_at_mcp():
    """A real client reaches the memory surface at exactly `/mcp` (the manifest
    path) and sees all five memory tools."""
    names = anyio.run(_list_tools, _app(), "/mcp")
    assert set(names) == {
        "remember",
        "recall",
        "memory_digest",
        "list_memories",
        "forget",
    }


def test_no_double_path_mount():
    """Regression: mounting `streamable_http_app()` at `/mcp` would serve the
    endpoint at `/mcp/mcp`. With the surface mounted at the prefix (`/`) the
    doubled path must 404, proving the real endpoint is `/mcp` (#28 lesson)."""
    app = _app()

    async def go() -> int:
        async with app.router.lifespan_context(app):
            async with _http(app) as http:
                return (await http.get("http://mem/mcp/mcp")).status_code

    assert anyio.run(go) == 404


def test_health_endpoint():
    """`/health` wins over the `/` mount and reports the plugin name."""
    app = _app()

    async def go() -> dict:
        async with app.router.lifespan_context(app):
            async with _http(app) as http:
                return (await http.get("http://mem/health")).json()

    body = anyio.run(go)
    assert body == {"status": "ok", "plugin": "memory"}
