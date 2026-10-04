"""Issue #240: after a restart the in-memory registry is empty until plugins
re-register on their heartbeat; a surface `tools/list` in that window holds
(bounded) for the previously-registered plugins that serve that surface, read
from a persisted state file."""

from __future__ import annotations

import json
import logging
import time

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from snowline_platform import registry_state
from snowline_platform.app import _startup_grace_lifespan, create_app
from snowline_platform.gateway_app import gateway_lifespan
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry, PluginStatus
from snowline_platform.registry_state import (
    RegistryStateFile,
    StartupGrace,
    read_registered,
    write_registered,
)
from snowline_platform.trust import Principal, TrustResolver

from ._gateway_helpers import InMemoryConnector, make_stub_plugin


class _AlwaysTrust:
    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _manifest(name: str, surfaces: dict[str, str] | None = None) -> PluginManifest:
    return PluginManifest(
        name=name,
        base_url=f"http://{name}",
        surfaces=surfaces if surfaces is not None else {"/mcp": "main"},
    )


def _expected(*manifests: PluginManifest) -> dict[str, PluginManifest]:
    return {m.name: m for m in manifests}


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(registry_state, "POLL_SECONDS", 0.01)


async def _timed(coro_fn, *args):
    start = anyio.current_time()
    await coro_fn(*args)
    return anyio.current_time() - start


# ---------------------------------------------------------------------------
# Registry observer hook.
# ---------------------------------------------------------------------------


def test_registry_emits_membership_changes_only():
    reg = PluginRegistry()
    seen = []
    reg.add_observer(seen.append)
    reg.upsert(_manifest("a"))
    reg.upsert(_manifest("a"))  # unchanged heartbeat: silent
    reg.upsert(PluginManifest(name="a", base_url="http://a2"))  # updated
    reg.set_status("a", PluginStatus.DOWN)  # status: not membership
    reg.unregister("a")
    assert [(c.kind, c.name) for c in seen] == [
        ("created", "a"),
        ("updated", "a"),
        ("removed", "a"),
    ]
    reg.remove_observer(seen.append)
    reg.upsert(_manifest("b"))
    assert len(seen) == 3


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


# ---------------------------------------------------------------------------
# StartupGrace.
# ---------------------------------------------------------------------------


def test_grace_waits_for_a_known_plugin():
    reg = PluginRegistry()
    grace = StartupGrace(reg, _expected(_manifest("governance")), window=5.0)

    async def _main():
        async def _register_later():
            await anyio.sleep(0.1)
            reg.upsert(_manifest("governance"))

        async with anyio.create_task_group() as tg:
            tg.start_soon(_register_later)
            return await _timed(grace.wait, "main")

    elapsed = anyio.run(_main)
    assert 0.05 < elapsed < 2.0


def test_grace_gives_up_after_the_window():
    reg = PluginRegistry()
    grace = StartupGrace(reg, _expected(_manifest("ghost")), window=0.2)
    elapsed = anyio.run(_timed, grace.wait, "main")
    assert 0.1 < elapsed < 1.0
    # Window over: never waits again, even though `ghost` is still missing.
    assert grace.pending() == frozenset()
    assert anyio.run(_timed, grace.wait, "main") < 0.05


def test_no_grace_without_a_state_file(tmp_path):
    expected = read_registered(tmp_path / "missing.json")
    assert expected == {}
    grace = StartupGrace(PluginRegistry(), expected, window=30.0)
    assert not grace.active()
    assert anyio.run(_timed, grace.wait, "main") < 0.05


def test_shadow_only_plugin_does_not_hold_main():
    reg = PluginRegistry()
    grace = StartupGrace(
        reg,
        _expected(
            _manifest("main-only"),
            _manifest("shadow-only", {"/shadow/mcp": "shadow"}),
        ),
        window=30.0,
    )
    reg.upsert(_manifest("main-only"))
    # main's only expected plugin is back → no wait, though shadow-only isn't.
    assert grace.pending_for("main") == frozenset()
    assert anyio.run(_timed, grace.wait, "main") < 0.05
    assert grace.pending_for("shadow") == {"shadow-only"}
    assert grace.pending() == {"shadow-only"}


def test_per_surface_wait_follows_allowlist_projection():
    """An allowlisted surface (issue #36/#38) projects each allowlisted plugin's
    `main` mapping, and excludes the rest — the grace mirrors that exactly."""
    grace = StartupGrace(
        PluginRegistry(),
        _expected(_manifest("governance"), _manifest("pm")),
        window=30.0,
    )
    core = frozenset({"governance"})
    assert grace.pending_for("core", core) == {"governance"}
    assert grace.pending_for("core", None) == frozenset()  # nothing maps 'core'
    assert grace.pending_for("main") == {"governance", "pm"}


def test_grace_window_reopens_on_start():
    now = [100.0]
    grace = StartupGrace(
        PluginRegistry(),
        _expected(_manifest("x")),
        window=10.0,
        clock=lambda: now[0],
    )
    now[0] = 115.0
    assert not grace.active()
    grace.start()
    assert grace.active()
    now[0] = 125.1
    assert not grace.active()


# ---------------------------------------------------------------------------
# State file.
# ---------------------------------------------------------------------------


def test_state_file_round_trip_and_hygiene(tmp_path):
    path = tmp_path / "nested" / "registered-plugins.json"
    reg = PluginRegistry()
    reg.add_observer(RegistryStateFile(reg, path).registry_changed)
    pm = _manifest("pm")
    gov = _manifest("governance", {"/mcp": "main", "/shadow/mcp": "shadow"})
    reg.upsert(pm)
    reg.upsert(gov)
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert sorted(data["plugins"]) == ["governance", "pm"]
    assert read_registered(path) == {"governance": gov, "pm": pm}
    reg.unregister("pm")
    assert set(read_registered(path)) == {"governance"}
    # Corrupt / wrong-shape files read as "nothing known" — never a boot error.
    path.write_text("{not json")
    assert read_registered(path) == {}
    path.write_text(json.dumps({"plugins": ["governance"]}))
    assert read_registered(path) == {}
    # One bad entry is skipped, the rest survive.
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": {
                    "pm": pm.model_dump(mode="json"),
                    "bad": {"name": "bad"},
                },
            }
        )
    )
    assert read_registered(path) == {"pm": pm}


def test_state_file_keeps_still_pending_plugins_during_grace(tmp_path):
    path = tmp_path / "registered-plugins.json"
    write_registered(
        path, [_manifest("governance"), _manifest("memory"), _manifest("pm")]
    )
    reg = PluginRegistry()
    grace = StartupGrace(reg, read_registered(path), window=30.0)
    reg.add_observer(RegistryStateFile(reg, path, grace).registry_changed)
    reg.upsert(_manifest("pm"))
    # A second restart now must still wait for governance + memory.
    assert set(read_registered(path)) == {"governance", "memory", "pm"}


def test_ghost_plugin_dropped_after_the_window(tmp_path):
    path = tmp_path / "registered-plugins.json"
    write_registered(path, [_manifest("governance"), _manifest("ghost")])
    reg = PluginRegistry()
    grace = StartupGrace(reg, read_registered(path), window=0.2)
    state = RegistryStateFile(reg, path, grace)
    reg.add_observer(state.registry_changed)
    reg.upsert(_manifest("governance"))
    assert set(read_registered(path)) == {"governance", "ghost"}  # in window

    anyio.run(grace.expire, state.write)
    assert set(read_registered(path)) == {"governance"}


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


# ---------------------------------------------------------------------------
# Through the app.
# ---------------------------------------------------------------------------


def _app(reg, path, servers):
    return create_app(
        resolver=TrustResolver([_AlwaysTrust()]),
        registry=reg,
        migrate_on_startup=False,
        connector=InMemoryConnector(servers),
        registry_state_file=path,
    )


async def _list_over_http(app, route="/mcp"):
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://platform",
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ) as http,
        streamable_http_client(
            f"http://platform{route}", http_client=http
        ) as (read, write, _sid),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return {t.name for t in (await session.list_tools()).tools}


def test_list_tools_waits_for_previously_registered_plugin(tmp_path):
    path = tmp_path / "registered-plugins.json"
    write_registered(path, [_manifest("alpha"), _manifest("beta")])
    reg = PluginRegistry()
    reg.upsert(_manifest("alpha"))
    app = _app(
        reg,
        path,
        {
            "http://alpha/mcp": make_stub_plugin("alpha", ["read"]),
            "http://beta/mcp": make_stub_plugin("beta", ["ping"]),
        },
    )
    assert set(app.state.startup_grace.expected) == {"alpha", "beta"}

    async def _main():
        async with (
            gateway_lifespan(app.state.gateway_mounts),
            _startup_grace_lifespan(app),
            anyio.create_task_group() as tg,
        ):

            async def _register_later():
                await anyio.sleep(0.3)
                reg.upsert(_manifest("beta"))

            tg.start_soon(_register_later)
            return await _list_over_http(app)

    names = anyio.run(_main)
    assert {"alpha__read", "beta__ping"} <= names


def test_app_lifespan_forgets_ghost_when_window_closes(tmp_path, monkeypatch):
    monkeypatch.setenv("SNOWLINE_STARTUP_GRACE_SECONDS", "0.2")
    path = tmp_path / "registered-plugins.json"
    write_registered(path, [_manifest("ghost")])
    reg = PluginRegistry()
    app = _app(reg, path, {})

    async def _main():
        async with _startup_grace_lifespan(app):
            # Boot's self-seed write kept the still-pending ghost.
            assert "ghost" in read_registered(path)
            await anyio.sleep(0.5)
            return set(read_registered(path))

    remaining = anyio.run(_main)
    assert "ghost" not in remaining
    assert "platform" in remaining


def test_grace_lifespan_shutdown_is_not_delayed(tmp_path):
    path = tmp_path / "registered-plugins.json"
    write_registered(path, [_manifest("ghost")])
    app = _app(PluginRegistry(), path, {})

    async def _main():
        async with _startup_grace_lifespan(app):
            pass

    assert app.state.startup_grace.window >= 1.0
    start = time.monotonic()
    anyio.run(_main)
    assert time.monotonic() - start < 1.0
