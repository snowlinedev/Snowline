"""The plugin's MCP surfaces served over the REAL streamable-HTTP transport — a
true `ClientSession` connecting to the MOUNTED app at `/mcp` and `/shadow/mcp`
exactly as the platform gateway does (driven in-process via httpx ASGITransport,
but the full transport stack runs).

This is the coverage gap that let a double-path mount bug ship: every other
governance MCP test drives the FastMCP server object IN-MEMORY
(`create_connected_server_and_client_session`), so the actual `app.mount(...)`
paths were never exercised — and `streamable_http_app()` already serves at its
own internal `/mcp`, so mounting it at `/mcp` had doubled the real endpoint to
`/mcp/mcp` while the manifest still advertised `/mcp`. These tests pin the served
endpoints to the manifest paths and prove shadow isolation holds over the wire.

No database needed: `list_tools` touches no store, migration + registration are
off, and the webhook delivery loop is disabled — so the lifespan only starts the
surfaces' session managers."""

from __future__ import annotations

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from snowline_governance.app import create_app


def _app(monkeypatch):
    monkeypatch.setenv("SNOWLINE_WEBHOOK_DISABLED", "1")
    return create_app(migrate_on_startup=False, register_on_startup=False)


def _http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gov",
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    )


async def _list_tools(app, route: str) -> list[str]:
    async with app.router.lifespan_context(app):
        async with _http(app) as http:
            async with streamable_http_client(
                f"http://gov{route}", http_client=http
            ) as (read, write, _sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return sorted(t.name for t in (await session.list_tools()).tools)


def test_main_surface_served_at_mcp(monkeypatch):
    """A real client reaches the `main` surface at exactly `/mcp` (the manifest
    path) and sees the decision + artifact tools."""
    names = anyio.run(_list_tools, _app(monkeypatch), "/mcp")
    assert "record_decision" in names
    assert "register_artifact" in names


def test_shadow_surface_served_at_shadow_mcp_and_isolated(monkeypatch):
    """The `shadow` surface is at exactly `/shadow/mcp` and carries shadow-write +
    read-real grounding but NOT the real-write verb — isolation over the wire."""
    names = anyio.run(_list_tools, _app(monkeypatch), "/shadow/mcp")
    assert "add_node" in names  # shadow write present
    assert "list_decisions" in names  # read-real grounding present
    assert "record_decision" not in names  # the isolation property, over HTTP


def test_no_double_path_mount(monkeypatch):
    """Regression: the buggy mount served the endpoint at `/mcp/mcp`. With the
    surfaces mounted at the prefix (`/` and `/shadow`) the doubled path must 404,
    proving the real endpoint is `/mcp`, not `/mcp/mcp`."""
    app = _app(monkeypatch)

    async def go() -> int:
        async with app.router.lifespan_context(app):
            async with _http(app) as http:
                return (await http.get("http://gov/mcp/mcp")).status_code

    assert anyio.run(go) == 404
