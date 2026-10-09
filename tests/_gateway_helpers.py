"""Shared test helpers for the gateway: an in-memory `UpstreamConnector` and a
tiny stub MCP plugin.

The production gateway connector talks streamable-HTTP to a plugin's
``base_url + plugin_path``. To exercise the aggregation + routing path WITHOUT
standing up HTTP servers, these helpers wire each upstream URL to an in-process
MCP server (a `FastMCP` stub OR a real governance surface) over the mcp lib's
in-memory transport (`create_connected_server_and_client_session`). The gateway
code under test is unchanged — only the connector seam is swapped, which is
exactly the abstraction `UpstreamConnector` exists for.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
import anyio
from mcp.shared.memory import create_client_server_memory_streams

from snowline_platform.gateway import InstructionsSession, Upstream


class InMemoryConnector:
    """An `UpstreamConnector` that maps an upstream URL → an in-process MCP server
    (FastMCP or low-level Server) and connects via the in-memory transport.

    `servers` maps ``base_url + plugin_path`` (i.e. `Upstream.url`) to the server
    object the gateway should aggregate. A `connect` to an unknown URL raises —
    standing in for an unreachable upstream (so a test can assert route-around
    behavior is driven by registry status, not silent URL misses)."""

    def __init__(self, servers: dict[str, object]) -> None:
        self._servers = servers
        # Every connect attempt's upstream URL, in order (cache-hit assertions).
        self.connects: list[str] = []

    @asynccontextmanager
    async def connect(self, upstream: Upstream):
        self.connects.append(upstream.url)
        server = self._servers.get(upstream.url)
        if server is None:
            raise ConnectionError(f"no in-memory upstream at {upstream.url!r}")
        if isinstance(server, FastMCP):
            server = server._mcp_server
        # Same wiring as `create_connected_server_and_client_session`, but with
        # the gateway's `InstructionsSession` so the upstream's initialize
        # instructions are captured exactly as in production.
        async with create_client_server_memory_streams() as (client, srv):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: server.run(
                        srv[0],
                        srv[1],
                        server.create_initialization_options(),
                        raise_exceptions=True,
                    )
                )
                try:
                    async with InstructionsSession(*client) as session:
                        await session.initialize()
                        yield session
                finally:
                    tg.cancel_scope.cancel()


def make_stub_plugin(
    name: str, tool_names: list[str], instructions: str | None = None
) -> FastMCP:
    """A tiny FastMCP plugin exposing `tool_names`, each an echo tool that returns
    a dict tagged with the plugin + tool (so a routed call is provably reaching
    THIS plugin's THIS tool). Stateless HTTP to match the plugin convention."""
    mcp = FastMCP(name, instructions=instructions, stateless_http=True)

    for tool_name in tool_names:

        def _make(tn: str):
            @mcp.tool(name=tn)
            async def _tool(value: str = "") -> dict:
                return {"plugin": name, "tool": tn, "echo": value}

            return _tool

        _make(tool_name)

    return mcp
