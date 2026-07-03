"""Gateway connect-phase retry (issue #58, deploy-continuity.md §3 — layer 2).

The safety line under test: a per-request upstream CONNECT failure (plugin
mid-kickstart) is retried briefly before the call fails, but ONLY the
connect+initialize phase — never anything after `call_tool` has been written
to the wire (a tool call is not idempotent). `list_tools` is read-only and may
retry across the whole connect+list attempt.

These monkeypatch `gateway.CONNECT_RETRY_BACKOFFS` down to near-zero so the
suite stays fast; the module-level constant exists precisely so tests can tune
it (deploy-continuity.md §3 "implementation-time tunable")."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import anyio
import pytest

from snowline_platform import gateway as gateway_module
from snowline_platform.gateway import GatewayError, SurfaceGateway
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry

from ._gateway_helpers import InMemoryConnector, make_stub_plugin


def _registry(*manifests: PluginManifest) -> PluginRegistry:
    reg = PluginRegistry()
    for m in manifests:
        reg.upsert(m)
    return reg


@pytest.fixture(autouse=True)
def _fast_backoffs(monkeypatch):
    """Tune the module-level retry policy down so retry tests stay fast."""
    monkeypatch.setattr(gateway_module, "CONNECT_RETRY_BACKOFFS", (0.001, 0.001))


class _FlakyConnector(InMemoryConnector):
    """An `InMemoryConnector` whose `connect` raises a connection-style error
    for the first `fail_times` attempts (standing in for a plugin mid-kickstart
    refusing connections), then behaves normally. `attempts` counts every call
    to `connect`, so a test can assert exactly how many connect attempts were
    made (initial + retries, never more than the retry policy allows)."""

    def __init__(self, servers: dict[str, object], fail_times: int) -> None:
        super().__init__(servers)
        self.attempts = 0
        self._fail_times = fail_times

    @asynccontextmanager
    async def connect(self, upstream):
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise ConnectionError(
                f"simulated connect failure #{self.attempts} (plugin mid-kickstart)"
            )
        async with super().connect(upstream) as session:
            yield session


class _CountingCallSession:
    """A stub `ClientSession`-shaped object whose `call_tool` always raises —
    standing in for a failure AFTER the call was written to the wire (a mid-call
    timeout, a malformed response, ...). `call_count` proves whether the gateway
    retried it (it must not)."""

    def __init__(self) -> None:
        self.call_count = 0

    async def call_tool(self, name: str, arguments: dict):
        self.call_count += 1
        raise ConnectionError("mid-call failure — must NOT be retried")


class _DirectConnector:
    """Connects straight to a pre-built session object (bypassing the real MCP
    in-memory transport) so a test can control exactly what `call_tool` does
    post-connect, independent of connect-phase behavior."""

    def __init__(self, session) -> None:
        self._session = session

    @asynccontextmanager
    async def connect(self, upstream):
        yield self._session


def test_call_tool_retries_connect_and_succeeds_transparently():
    """A connect that fails twice (exactly the retry budget) then succeeds is
    invisible to the caller: `call_tool` returns the normal result."""
    alpha = make_stub_plugin("alpha", ["echo"])
    reg = _registry(
        PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"})
    )
    connector = _FlakyConnector({"http://alpha/mcp": alpha}, fail_times=2)
    gw = SurfaceGateway(reg, "main", connector)

    res = anyio.run(gw.call_tool, "alpha__echo", {"value": "hi"})

    assert res.isError is not True
    assert connector.attempts == 3  # 1 initial + 2 retries, then success


def test_list_tools_retries_connect_and_succeeds_transparently():
    """list_tools may retry connect+list in full since it's read-only."""
    alpha = make_stub_plugin("alpha", ["echo"])
    reg = _registry(
        PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"})
    )
    connector = _FlakyConnector({"http://alpha/mcp": alpha}, fail_times=2)
    gw = SurfaceGateway(reg, "main", connector)

    names = {t.name for t in anyio.run(gw.list_tools)}

    assert names == {"alpha__echo"}
    assert connector.attempts == 3


def test_call_tool_exhausted_retries_raises_gateway_error_with_warning(caplog):
    """More failures than the retry budget -> a clear GatewayError (not a hang),
    with a WARNING naming the plugin + surface once retries are exhausted."""
    alpha = make_stub_plugin("alpha", ["echo"])
    reg = _registry(
        PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"})
    )
    # 3 failures > the 2-retry budget -> never recovers within this call.
    connector = _FlakyConnector({"http://alpha/mcp": alpha}, fail_times=3)
    gw = SurfaceGateway(reg, "main", connector)

    with caplog.at_level(logging.WARNING, logger="snowline_platform.gateway"):
        with pytest.raises(GatewayError):
            anyio.run(gw.call_tool, "alpha__echo", {"value": "hi"})

    # Exactly 1 initial + 2 retries -> exhausted, no more attempts beyond that.
    assert connector.attempts == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "alpha" in warnings[0].message
    assert "main" in warnings[0].message


def test_call_tool_does_not_retry_after_call_is_delivered():
    """A failure raised BY call_tool itself (post-write) must NEVER be retried:
    the upstream session's `call_tool` is invoked exactly once, and the raw
    exception propagates un-wrapped (today's existing post-write behavior),
    not converted into a retried/route-arounded GatewayError."""
    session = _CountingCallSession()
    reg = _registry(
        PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"})
    )
    connector = _DirectConnector(session)
    gw = SurfaceGateway(reg, "main", connector)

    with pytest.raises(ConnectionError):
        anyio.run(gw.call_tool, "alpha__echo", {"value": "hi"})

    assert session.call_count == 1


def test_call_tool_exhausted_retries_do_not_exceed_policy(monkeypatch):
    """Retries are bounded exactly by `CONNECT_RETRY_BACKOFFS`'s length — even a
    connector that always fails only ever sees 1 + len(backoffs) attempts."""
    monkeypatch.setattr(gateway_module, "CONNECT_RETRY_BACKOFFS", (0.001, 0.001, 0.001))
    alpha = make_stub_plugin("alpha", ["echo"])
    reg = _registry(
        PluginManifest(name="alpha", base_url="http://alpha", surfaces={"/mcp": "main"})
    )
    connector = _FlakyConnector({"http://alpha/mcp": alpha}, fail_times=999)
    gw = SurfaceGateway(reg, "main", connector)

    with pytest.raises(GatewayError):
        anyio.run(gw.call_tool, "alpha__echo", {})

    assert connector.attempts == 4  # 1 initial + 3 retries (tuned policy), no more
