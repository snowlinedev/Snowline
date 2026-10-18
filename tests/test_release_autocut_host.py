"""The in-process auto-cut host (item a25fa809): release/autocut_host.py.

The cut itself (`run_autocut`) has its own suite (test_release_autocut.py);
here a fake `run` callable stands in for it and a fake sleep/clock drives the
loop — what is under test is the HOST: the marker switch, the non-configurable
refusals, the clean env, the loop's survival, and shutdown behaviour (cancel a
sleep at once, wait — bounded — for a cut in flight). Plus `--status` against a
real flock, and the platform lifespan starting/stopping the host.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from starlette.testclient import TestClient

from snowline_platform import standby
from snowline_platform.release import autocut as a
from snowline_platform.release import autocut_host as h
from snowline_platform.release import runner as runner_mod

HUB = "http://127.0.0.1:8850"


@pytest.fixture()
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    (path / ".config" / "snowline").mkdir(parents=True)
    return path


@pytest.fixture()
def root(tmp_path) -> Path:
    path = tmp_path / "checkout"
    (path / "release").mkdir(parents=True)
    (path / "release" / "components.json").write_text("{}")
    (path / ".git").mkdir()
    return path


def enable(home: Path) -> None:
    a.enable_marker_path(home).touch()


class FakeRun:
    def __init__(self, outcomes=("noop",), *, raises=None):
        self.calls: list[h.TickSettings] = []
        self.outcomes = list(outcomes)
        self.raises = list(raises or [])

    def __call__(self, settings):
        self.calls.append(settings)
        if self.raises:
            exc = self.raises.pop(0)
            if exc is not None:
                raise exc
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]


def make_host(home, root, run, *, environ=None, is_local=lambda _h: True, **kw):
    return h.AutocutHost(
        home=home,
        run=run,
        self_url=lambda: HUB,
        root=root,
        is_local=is_local,
        environ=environ if environ is not None else {
            "SNOWLINE_PLATFORM_URL": HUB,
            "PATH": "/usr/bin:/bin",
            "HOME": "/ignored",
            "USER": "sean",
            "SNOWLINE_PLATFORM_DATABASE_URL": "postgresql+psycopg:///snowline_platform",
        },
        venv_bin=Path("/repo/.venv/bin"),
        **kw,
    )


def tick(host) -> str:
    return anyio.run(host.tick)


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------


def test_no_marker_never_runs(home, root):
    run = FakeRun()
    host = make_host(home, root, run)
    assert tick(host) == h.OUTCOME_DISABLED
    assert run.calls == []
    assert host.state.enabled is False


def test_marker_runs_on_tick_with_the_env_loaded(home, root):
    enable(home)
    a.env_file_path(home).write_text(
        "# scratch DBs for the cut's test runs\n"
        "export SNOWLINE_PLATFORM_TEST_DATABASE_URL=postgresql+psycopg:///scratch\n"
        "FOO='bar baz'\n"
    )
    run = FakeRun(["ok"])
    host = make_host(home, root, run)
    assert tick(host) == "ok"
    assert host.state.last_outcome == "ok"
    assert host.state.running is False
    (settings,) = run.calls
    env = settings.env
    assert settings.platform_url == HUB and settings.root == root and settings.home == home
    assert env["SNOWLINE_PLATFORM_TEST_DATABASE_URL"] == "postgresql+psycopg:///scratch"
    assert env["FOO"] == "bar baz"
    assert env["SNOWLINE_PLATFORM_URL"] == HUB
    assert env["HOME"] == str(home)
    assert env["USER"] == "sean"
    # The daemon's LIVE database URL never reaches a cut's subprocesses.
    assert "SNOWLINE_PLATFORM_DATABASE_URL" not in env
    path = env["PATH"].split(":")
    assert path[:2] == [str(home / ".local" / "bin"), h.HOMEBREW_BIN]
    assert "/usr/bin" in path and path[-1] == "/repo/.venv/bin"


def test_marker_is_re_read_every_tick(home, root):
    run = FakeRun()
    host = make_host(home, root, run)
    assert tick(host) == h.OUTCOME_DISABLED
    enable(home)
    assert tick(host) == "noop"
    a.enable_marker_path(home).unlink()
    assert tick(host) == h.OUTCOME_DISABLED
    assert len(run.calls) == 1


def test_kill_switch_wins_over_the_marker(home, root):
    enable(home)
    a.disable_marker_path(home).touch()
    run = FakeRun()
    host = make_host(home, root, run)
    assert tick(host) == h.OUTCOME_DISABLED
    assert "kill switch" in host.state.last_detail
    assert run.calls == []


# --------------------------------------------------------------------------
# Refusals (not configurable)
# --------------------------------------------------------------------------


def test_spoke_role_refuses(home, root):
    enable(home)
    (home / ".config" / "snowline" / "stack.json").write_text(json.dumps({
        "schema_version": 1, "role": "spoke", "instance_id": "MBP",
        "primary_tailnet_address": "100.81.176.75",
    }))
    run = FakeRun()
    host = make_host(home, root, run)
    assert tick(host) == h.OUTCOME_REFUSED
    assert "role=spoke" in host.state.last_detail
    assert run.calls == []


def test_platform_url_not_this_platform_refuses(home, root):
    enable(home)
    a.env_file_path(home).write_text("SNOWLINE_PLATFORM_URL=http://100.81.176.75:8850\n")
    run = FakeRun()
    host = make_host(home, root, run)  # this platform is HUB (127.0.0.1)
    assert tick(host) == h.OUTCOME_REFUSED
    assert "not this platform's own address" in host.state.last_detail
    assert run.calls == []


def test_platform_url_on_another_machine_refuses(home, root):
    enable(home)
    run = FakeRun()
    host = make_host(home, root, run, is_local=lambda _h: False)
    assert tick(host) == h.OUTCOME_REFUSED
    assert "not an address of this machine" in host.state.last_detail
    assert run.calls == []


def test_unset_platform_url_refuses(home, root):
    enable(home)
    run = FakeRun()
    host = make_host(home, root, run, environ={"PATH": "/usr/bin"})
    assert tick(host) == h.OUTCOME_REFUSED
    assert "unset" in host.state.last_detail


def test_not_source_run_refuses(home):
    enable(home)
    run = FakeRun()
    host = make_host(home, None, run)
    assert tick(host) == h.OUTCOME_REFUSED
    assert "not running from a git checkout" in host.state.last_detail
    assert run.calls == []


def test_real_loopback_is_local():
    assert h.is_local_address("127.0.0.1") is True
    assert h.is_local_address("192.0.2.1") is False  # TEST-NET-1


def test_refusal_warns_once_per_change_not_every_tick(home, root, caplog):
    enable(home)
    a.env_file_path(home).write_text("SNOWLINE_PLATFORM_URL=http://elsewhere:8850\n")
    host = make_host(home, root, FakeRun())
    with caplog.at_level(logging.WARNING, logger="snowline_platform.autocut_host"):
        tick(host)
        tick(host)
        tick(host)
    assert len([r for r in caplog.records if "REFUSED" in r.getMessage()]) == 1


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


class Clock:
    """Fake sleep + now: each sleep advances time; after `ticks` sleeps the
    next one parks forever (so the test can cancel a quiescent loop)."""

    def __init__(self, ticks: int):
        self.t = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []
        self.ticks = ticks
        self.parked = anyio.Event()

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) > self.ticks:
            self.parked.set()
            await anyio.sleep_forever()
        self.t += timedelta(seconds=seconds)
        await anyio.lowlevel.checkpoint()


def run_loop(host, clock):
    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: host.loop(interval_s=3600, startup_delay_s=180, sleep=clock.sleep)
            )
            await clock.parked.wait()
            assert host.state.alive is True
            tg.cancel_scope.cancel()

    anyio.run(main)


def test_loop_sleeps_the_startup_delay_then_ticks_hourly(home, root):
    enable(home)
    run = FakeRun()
    clock = Clock(ticks=3)
    host = make_host(home, root, run, now=clock.now)
    run_loop(host, clock)
    assert clock.sleeps[:3] == [180, 3600, 3600]
    assert len(run.calls) == 3
    assert host.state.alive is False and host.state.next_tick_at is None


def test_a_run_that_raises_does_not_kill_the_loop(home, root, caplog):
    enable(home)
    run = FakeRun(["ok"], raises=[RuntimeError("boom"), None])
    clock = Clock(ticks=2)
    host = make_host(home, root, run, now=clock.now)
    with caplog.at_level(logging.ERROR, logger="snowline_platform.autocut_host"):
        run_loop(host, clock)
    assert len(run.calls) == 2
    assert host.state.last_outcome == "ok"
    assert any("tick raised" in r.getMessage() for r in caplog.records)


def test_shutdown_during_the_sleep_cancels_promptly(home, root):
    host = make_host(home, root, FakeRun())

    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: host.loop(interval_s=3600, startup_delay_s=3600))
            await anyio.sleep(0.05)
            tg.cancel_scope.cancel()

    started = time.monotonic()
    anyio.run(main)
    assert time.monotonic() - started < 2
    assert host.state.alive is False


class BlockingRun:
    def __init__(self, seconds):
        self.seconds = seconds
        self.release = threading.Event()
        self.finished = threading.Event()

    def __call__(self, settings):
        self.release.wait(self.seconds)
        self.finished.set()
        return "ok"


def _cancel_mid_run(host, run):
    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: host.loop(interval_s=3600, startup_delay_s=0))
            while not host.state.running:
                await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

    started = time.monotonic()
    anyio.run(main)
    return time.monotonic() - started


def test_shutdown_during_a_run_waits_for_it(home, root):
    enable(home)
    run = BlockingRun(0.5)
    host = make_host(home, root, run, shutdown_wait_s=30)
    elapsed = _cancel_mid_run(host, run)
    assert run.finished.is_set(), "shutdown returned before the cut finished"
    assert 0.4 <= elapsed < 10
    assert host.state.running is False


def test_shutdown_wait_is_bounded(home, root):
    enable(home)
    run = BlockingRun(30)
    host = make_host(home, root, run, shutdown_wait_s=0.3)
    try:
        elapsed = _cancel_mid_run(host, run)
        assert not run.finished.is_set()
        assert elapsed < 5
    finally:
        run.release.set()  # free the abandoned worker thread


# --------------------------------------------------------------------------
# Runner.base_env (the clean env reaches the subprocesses)
# --------------------------------------------------------------------------


def test_runner_base_env_replaces_the_process_env(monkeypatch):
    monkeypatch.setenv("SNOWLINE_LEAK", "live")
    runner = runner_mod.Runner(base_env={"PATH": "/usr/bin:/bin", "ONLY": "1"})
    out = runner.read(["/bin/sh", "-c", 'echo "${ONLY}:${SNOWLINE_LEAK:-none}:${X}"'], env={"X": "2"})
    assert out.strip() == "1:none:2"


# --------------------------------------------------------------------------
# `snowline release autocut --status`
# --------------------------------------------------------------------------


def _status(monkeypatch, capsys, home) -> str:
    from snowline_platform.cli import main

    monkeypatch.setenv("HOME", str(home))
    assert main(["release", "autocut", "--status"]) == 0
    return capsys.readouterr().out


def test_status_lock_free_no_marker_no_report(monkeypatch, capsys, home):
    out = _status(monkeypatch, capsys, home)
    assert "lock:     free" in out
    assert "no enable marker" in out
    assert "no report" in out
    assert not a.lock_path(home).exists(), "--status must not create the lock"


def test_status_shows_a_held_lock_and_the_latest_report(monkeypatch, capsys, home):
    enable(home)
    rpt = a.AutocutReport(started_at="2026-10-18T10:00:00+00:00", outcome="ok", detail="cut v0.5.3")
    rpt.finished_at = "2026-10-18T10:06:00+00:00"
    rpt.version = "v0.5.3"
    rpt.cut_attempted = True
    a.write_report(home, rpt)
    lock = a._Lock(a.lock_path(home))  # the real flock a run takes
    assert lock.acquire()
    try:
        out = _status(monkeypatch, capsys, home)
    finally:
        lock.release()
    assert "HELD by pid" in out and "do NOT restart" in out
    assert "enabled" in out
    assert "last run: ok at 2026-10-18T10:06:00+00:00 — cut v0.5.3" in out
    assert "last cut: v0.5.3 (ok)" in out
    # Released: free again.
    assert a.lock_state(home).held is False


# --------------------------------------------------------------------------
# The standby surface + the platform lifespan
# --------------------------------------------------------------------------


def test_standby_autocut_carries_the_live_running_flag():
    state = h.HostState(enabled=True, running=True, next_tick_at="2026-10-18T13:00:00Z")
    merged = standby.with_live_host({"last_outcome": "noop", "at": "x", "detail": None, "last_cut": None}, state)
    assert merged["running"] is True and merged["host_enabled"] is True
    assert merged["next_tick_at"] == "2026-10-18T13:00:00Z"
    assert merged["last_outcome"] == "noop"
    assert standby.with_live_host(None, state)["running"] is True
    assert standby.with_live_host({"last_outcome": "ok"}, None) == {"last_outcome": "ok"}


def test_lifespan_starts_and_stops_the_host(tmp_path, monkeypatch):
    from snowline_platform.app import create_app
    from snowline_platform.trust import Principal, TrustResolver

    class _Trust:
        def resolve(self, peer_ip, headers):
            return Principal(id="t", source="test")

    home = Path(str(tmp_path / "_autocut_home"))  # conftest's SNOWLINE_AUTOCUT_HOME
    (home / ".config" / "snowline").mkdir(parents=True)
    enable(home)
    ran = threading.Event()

    def fake_run(settings):
        ran.set()
        return "noop"

    app = create_app(resolver=TrustResolver([_Trust()]), migrate_on_startup=False)
    app.state.autocut_host_options = {
        "run": fake_run,
        "startup_delay_s": 0,
        "root": tmp_path,
        "self_url": lambda: HUB,
        "is_local": lambda _h: True,
        "environ": {"SNOWLINE_PLATFORM_URL": HUB},
    }
    with TestClient(app):
        assert ran.wait(5), "the host never ticked"
        state = app.state.autocut_host
        assert state.alive is True
        deadline = time.monotonic() + 5
        while state.last_outcome != "noop" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert state.last_outcome == "noop"
    assert state.alive is False


def test_run_once_drives_run_autocut_with_the_clean_env(monkeypatch, home):
    seen = {}

    def fake_run_autocut(config, *, checkouts, home, runner, platform_url, report):
        seen.update(checkouts=checkouts, home=home, env=runner.base_env, url=platform_url)
        return a.AutocutReport(started_at="t", outcome="noop")

    monkeypatch.setattr(a, "run_autocut", fake_run_autocut)
    repo = Path(__file__).resolve().parents[1]
    settings = h.TickSettings(home=home, root=repo, platform_url=HUB, env={"PATH": "/usr/bin", "ONLY": "1"})
    assert h.run_once(settings).outcome == "noop"
    assert seen["env"] == {"PATH": "/usr/bin", "ONLY": "1"}
    assert seen["url"] == HUB and seen["home"] == home
    assert seen["checkouts"]["platform"] == repo
