"""Issue #240: gateway surfaces push `notifications/tools/list_changed` when the
registry changes their upstream set, and hold `tools/list` briefly after a
restart until previously-registered plugins are back.

The end-to-end test runs the platform app under a REAL uvicorn socket server:
httpx's `ASGITransport` buffers whole responses, so it can never deliver a
server-pushed message on the long-lived standalone GET stream — the very thing
under test."""

from __future__ import annotations

import json
import logging

import anyio
import httpx
import pytest
import uvicorn
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from snowline_platform import gateway_notify, registry_state
from snowline_platform.app import create_app
from snowline_platform.gateway_app import gateway_lifespan
from snowline_platform.gateway_notify import SessionSet, SurfaceChangeNotifier
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry, PluginStatus
from snowline_platform.registry_state import (
    RegistryStateFile,
    StartupGrace,
    read_registered_names,
    write_registered_names,
)
from snowline_platform.trust import Principal, TrustResolver

from ._gateway_helpers import InMemoryConnector, make_stub_plugin


class _AlwaysTrust:
    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _manifest(name: str) -> PluginManifest:
    return PluginManifest(
        name=name, base_url=f"http://{name}", surfaces={"/mcp": "main"}
    )


@pytest.fixture
def fast_debounce(monkeypatch):
    monkeypatch.setattr(gateway_notify, "DEBOUNCE_SECONDS", 0.05)


# ---------------------------------------------------------------------------
# End to end: a live stateful client is told, and re-lists the new tools.
# ---------------------------------------------------------------------------


async def _serve(app, fn):
    """Serve `app` on an ephemeral loopback port (gateway lifespan entered by
    hand; the DB-migrating app lifespan is off) and run `fn(base_url)`."""
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=0, lifespan="off", log_level="warning"
        )
    )
    async with (
        gateway_lifespan(app.state.gateway_mounts),
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(server.serve)
        with anyio.fail_after(10):
            while not server.started:
                await anyio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            return await fn(f"http://127.0.0.1:{port}")
        finally:
            server.should_exit = True


def test_live_client_gets_list_changed_and_sees_new_tools(fast_debounce):
    reg = PluginRegistry()
    reg.upsert(_manifest("alpha"))
    connector = InMemoryConnector(
        {
            "http://alpha/mcp": make_stub_plugin("alpha", ["read"]),
            "http://beta/mcp": make_stub_plugin("beta", ["ping"]),
        }
    )
    app = create_app(
        resolver=TrustResolver([_AlwaysTrust()]),
        registry=reg,
        migrate_on_startup=False,
        connector=connector,
    )
    main = next(m for m in app.state.gateway_mounts if m.route == "/mcp")
    changed = anyio.Event()

    async def _on_message(message) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root, types.ToolListChangedNotification
        ):
            changed.set()

    async def _client(base: str):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), follow_redirects=True
        ) as http, streamable_http_client(
            f"{base}/mcp", http_client=http
        ) as (read, write, get_sid), ClientSession(
            read, write, message_handler=_on_message
        ) as session:
            init = await session.initialize()
            assert init.capabilities.tools is not None
            assert init.capabilities.tools.listChanged is True
            assert get_sid() is not None  # stateful: a session id

            before = {t.name for t in (await session.list_tools()).tools}
            assert "alpha__read" in before
            assert "beta__ping" not in before
            assert len(main.sessions) == 1

            # A plugin registers mid-session (the post-restart heartbeat).
            reg.upsert(_manifest("beta"))
            with anyio.fail_after(5):
                await changed.wait()

            after = {t.name for t in (await session.list_tools()).tools}
            assert {"alpha__read", "beta__ping"} <= after
        # Client gone (DELETE) → its session leaves the notification set.
        with anyio.fail_after(5):
            while len(main.sessions):
                await anyio.sleep(0.02)

    anyio.run(_serve, app, _client)


# ---------------------------------------------------------------------------
# Notifier: debounce, change filtering, session hygiene.
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, exc: BaseException | None = None, hang: bool = False):
        self.exc = exc
        self.hang = hang
        self.sent = 0

    async def send_tool_list_changed(self) -> None:
        if self.hang:
            await anyio.sleep_forever()
        if self.exc is not None:
            raise self.exc
        self.sent += 1


async def _with_notifier(reg, sessions, fn, surface="main"):
    notifier = SurfaceChangeNotifier(reg, surface, sessions)
    reg.add_observer(notifier.registry_changed)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(notifier.run)
            await anyio.lowlevel.checkpoint()  # let run() capture the loop
            await fn(notifier)
            tg.cancel_scope.cancel()
    finally:
        reg.remove_observer(notifier.registry_changed)
    return notifier


def test_burst_is_coalesced_into_one_notification(fast_debounce):
    reg = PluginRegistry()
    sessions = SessionSet()
    good = _FakeSession()
    sessions.add(good)

    async def _fn(notifier):
        for name in ("a", "b", "c", "d", "e"):
            reg.upsert(_manifest(name))
        await anyio.sleep(0.2)
        assert notifier.broadcasts == 1
        assert good.sent == 1
        # A later change is a NEW burst → a second notification.
        reg.unregister("c")
        await anyio.sleep(0.2)
        assert good.sent == 2

    anyio.run(_with_notifier, reg, sessions, _fn)


def test_no_notification_when_surface_upstreams_unchanged(fast_debounce):
    reg = PluginRegistry()
    reg.upsert(_manifest("a"))
    sessions = SessionSet()
    good = _FakeSession()
    sessions.add(good)

    async def _fn(notifier):
        reg.upsert(_manifest("a"))  # heartbeat: "unchanged"
        reg.set_status("a", PluginStatus.UP)  # UNKNOWN→UP: still routable
        # A plugin mapped only onto ANOTHER surface doesn't touch `main`.
        reg.upsert(
            PluginManifest(
                name="s", base_url="http://s", surfaces={"/shadow/mcp": "shadow"}
            )
        )
        await anyio.sleep(0.2)
        assert good.sent == 0
        # UP→DOWN drops it from the surface: that IS a change.
        reg.set_status("a", PluginStatus.DOWN)
        await anyio.sleep(0.2)
        assert good.sent == 1

    anyio.run(_with_notifier, reg, sessions, _fn)


def test_dead_session_does_not_break_the_others(fast_debounce, monkeypatch):
    monkeypatch.setattr(gateway_notify, "SEND_TIMEOUT_SECONDS", 0.1)
    reg = PluginRegistry()
    sessions = SessionSet()
    closed = _FakeSession(exc=anyio.ClosedResourceError())
    broken = _FakeSession(exc=anyio.BrokenResourceError())
    weird = _FakeSession(exc=RuntimeError("boom"))
    wedged = _FakeSession(hang=True)
    good = _FakeSession()
    for s in (closed, broken, weird, wedged, good):
        sessions.add(s)

    async def _fn(notifier):
        _, outcome = reg.upsert(_manifest("a"))
        assert outcome == "created"  # the registry write stands regardless
        await anyio.sleep(0.4)
        assert good.sent == 1
        remaining = set(sessions.snapshot())
        assert closed not in remaining and broken not in remaining
        assert {weird, wedged, good} <= remaining

    anyio.run(_with_notifier, reg, sessions, _fn)
    assert reg.get("a").manifest.name == "a"


def test_failing_observer_never_fails_a_registration(caplog):
    reg = PluginRegistry()
    seen = []

    def _bad(change):
        raise RuntimeError("observer blew up")

    reg.add_observer(_bad)
    reg.add_observer(seen.append)
    with caplog.at_level(logging.ERROR):
        _, outcome = reg.upsert(_manifest("a"))
    assert outcome == "created"
    assert [c.kind for c in seen] == ["created"]
    assert "observer" in caplog.text


def test_registry_emits_only_routability_changes():
    reg = PluginRegistry()
    seen = []
    reg.add_observer(seen.append)
    reg.upsert(_manifest("a"))
    reg.upsert(_manifest("a"))  # unchanged heartbeat: silent
    reg.upsert(PluginManifest(name="a", base_url="http://a2"))  # updated
    reg.set_status("a", PluginStatus.UP)  # UNKNOWN→UP: silent
    reg.set_status("a", PluginStatus.DOWN)
    reg.set_status("a", PluginStatus.DOWN)  # no flip: silent
    reg.set_status("a", PluginStatus.UP)
    reg.unregister("a")
    assert [(c.kind, c.name) for c in seen] == [
        ("created", "a"),
        ("updated", "a"),
        ("status", "a"),
        ("status", "a"),
        ("removed", "a"),
    ]
    reg.remove_observer(seen.append)
    reg.upsert(_manifest("b"))
    assert len(seen) == 5


def test_notifier_before_run_is_inert():
    """create_app seeds the registry before any loop runs; a notifier that
    isn't running yet must take the change silently."""
    reg = PluginRegistry()
    notifier = SurfaceChangeNotifier(reg, "main", SessionSet())
    reg.add_observer(notifier.registry_changed)
    reg.upsert(_manifest("a"))  # no loop, no raise
    assert notifier.broadcasts == 0


# ---------------------------------------------------------------------------
# Startup grace + the registered-plugins state file.
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(registry_state, "POLL_SECONDS", 0.01)


def test_grace_waits_for_a_known_plugin(fast_poll):
    reg = PluginRegistry()
    grace = StartupGrace(reg, frozenset({"governance"}), window=5.0)

    async def _main():
        async def _register_later():
            await anyio.sleep(0.1)
            reg.upsert(_manifest("governance"))

        async with anyio.create_task_group() as tg:
            tg.start_soon(_register_later)
            start = anyio.current_time()
            await grace.wait()
            elapsed = anyio.current_time() - start
        assert 0.05 < elapsed < 2.0
        assert {e.manifest.name for e in reg.list()} == {"governance"}

    anyio.run(_main)


def test_grace_gives_up_after_the_window(fast_poll):
    reg = PluginRegistry()
    grace = StartupGrace(reg, frozenset({"ghost"}), window=0.2)

    async def _main():
        start = anyio.current_time()
        await grace.wait()
        return anyio.current_time() - start

    elapsed = anyio.run(_main)
    assert 0.1 < elapsed < 1.0
    # Window over: never waits again, even though `ghost` is still missing.
    assert grace.pending() == frozenset()
    assert anyio.run(_main) < 0.05


def test_no_grace_without_a_state_file(tmp_path):
    expected = read_registered_names(tmp_path / "missing.json")
    assert expected == frozenset()
    grace = StartupGrace(PluginRegistry(), expected, window=30.0)
    assert not grace.active()

    async def _main():
        start = anyio.current_time()
        await grace.wait()
        return anyio.current_time() - start

    assert anyio.run(_main) < 0.05


def test_grace_window_reopens_on_start():
    now = [100.0]
    grace = StartupGrace(
        PluginRegistry(), frozenset({"x"}), window=10.0, clock=lambda: now[0]
    )
    now[0] = 115.0
    assert not grace.active()
    grace.start()
    assert grace.active()
    now[0] = 125.1
    assert not grace.active()


def test_state_file_round_trip_and_hygiene(tmp_path):
    path = tmp_path / "nested" / "registered-plugins.json"
    reg = PluginRegistry()
    reg.add_observer(RegistryStateFile(reg, path).registry_changed)
    reg.upsert(_manifest("pm"))
    reg.upsert(_manifest("governance"))
    assert json.loads(path.read_text()) == {
        "version": 1,
        "plugins": ["governance", "pm"],
    }
    reg.unregister("pm")
    assert read_registered_names(path) == frozenset({"governance"})
    # Corrupt / wrong-shape files read as "nothing known" — never a boot error.
    path.write_text("{not json")
    assert read_registered_names(path) == frozenset()
    path.write_text(json.dumps({"plugins": "governance"}))
    assert read_registered_names(path) == frozenset()


def test_state_file_keeps_still_pending_names_during_grace(tmp_path):
    path = tmp_path / "registered-plugins.json"
    write_registered_names(path, {"governance", "memory", "pm"})
    reg = PluginRegistry()
    grace = StartupGrace(reg, read_registered_names(path), window=30.0)
    reg.add_observer(RegistryStateFile(reg, path, grace).registry_changed)
    reg.upsert(_manifest("pm"))
    # A second restart now must still wait for governance + memory.
    assert read_registered_names(path) == frozenset(
        {"governance", "memory", "pm"}
    )


def test_state_file_write_failure_is_logged_not_raised(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    reg = PluginRegistry()
    reg.add_observer(
        RegistryStateFile(reg, blocker / "sub" / "state.json").registry_changed
    )
    with caplog.at_level(logging.WARNING):
        _, outcome = reg.upsert(_manifest("a"))
    assert outcome == "created"
    assert "failed to write" in caplog.text


def test_list_tools_waits_for_previously_registered_plugin(tmp_path, fast_poll):
    """Through the app: a state file naming `beta` makes the first tools/list
    after boot wait until beta re-registers, then include its tools."""
    path = tmp_path / "registered-plugins.json"
    write_registered_names(path, {"alpha", "beta", "platform"})
    reg = PluginRegistry()
    reg.upsert(_manifest("alpha"))
    connector = InMemoryConnector(
        {
            "http://alpha/mcp": make_stub_plugin("alpha", ["read"]),
            "http://beta/mcp": make_stub_plugin("beta", ["ping"]),
        }
    )
    app = create_app(
        resolver=TrustResolver([_AlwaysTrust()]),
        registry=reg,
        migrate_on_startup=False,
        connector=connector,
        registry_state_file=path,
    )
    assert app.state.startup_grace.expected == {"alpha", "beta", "platform"}

    async def _main():
        async with gateway_lifespan(app.state.gateway_mounts):
            app.state.startup_grace.start()

            async def _register_later():
                await anyio.sleep(0.3)
                reg.upsert(_manifest("beta"))

            async with anyio.create_task_group() as tg:
                tg.start_soon(_register_later)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://platform",
                    timeout=httpx.Timeout(30.0),
                    follow_redirects=True,
                ) as http, streamable_http_client(
                    "http://platform/mcp", http_client=http
                ) as (read, write, _sid), ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
        return {t.name for t in result.tools}

    names = anyio.run(_main)
    assert {"alpha__read", "beta__ping"} <= names
