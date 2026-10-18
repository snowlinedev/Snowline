"""The auto-cut hosted INSIDE the platform daemon (item a25fa809).

Why: decision 9ce14c69 makes the hub the test seat that agents deploy
continuously, with standby currency handled by machinery, never a human step.
Registering a NEW launchd job is the one deploy step an agent session cannot
take, so `dev.snowline.autocut.plist` (#258/#292) brought back exactly the
human `sudo install-daemons.sh` that decision removed. Hosting the same
`run_autocut` as a background task of the platform the deploy chain already
restarts makes enabling it a pull + restart plus one marker file.

One tick (hourly, after a startup delay):

  switches   ~/.config/snowline/autocut.enabled must exist and
             autocut.disabled (the shared kill switch) must not. Re-read
             every tick: touching/removing a file flips the host without a
             restart.
  refusals   NOT configurable. Refuse (WARNING once per change of reason, not
             every tick) when this machine is a packaged spoke (stack.json
             role=spoke), when the gates' SNOWLINE_PLATFORM_URL is not this
             platform's own address, or when the platform is not running
             from a git checkout (the hub is source-run; a wheel has no
             checkouts to cut from).
  env        a CLEAN subprocess env — the daemon's own env carries the LIVE
             database URLs, which a cut's test runs must never see — built
             like the launchd timer's (HOME/USER/PATH + SNOWLINE_PLATFORM_URL),
             with ~/.config/snowline/autocut.env (KEY=VALUE lines, never
             executed) layered on top and PATH extended for uv/gh/npm.
  run        `autocut.run_autocut` in a worker thread (the cut is ~6 minutes
             of blocking subprocesses; the event loop keeps serving). Its
             flock makes a coexisting launchd timer harmless: whichever fires
             second records `locked`.
  outcome    one INFO line per run (noop/ok/gated/failed/locked/...); the
             report stays where #292 writes it (autocut-report.json).

The loop never dies: anything a tick raises is logged and the next tick runs.
Shutdown cancels the sleep at once; a cut in flight is WAITED FOR (bounded by
`shutdown_wait_s`, default 10 minutes) rather than abandoned half-published —
so a platform restart during a cut takes up to that long. `snowline release
autocut --status` says whether a cut is running before you restart.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import anyio

from . import autocut as a

log = logging.getLogger("snowline_platform.autocut_host")

DEFAULT_INTERVAL_S = 3600.0
DEFAULT_STARTUP_DELAY_S = 180.0
DEFAULT_SHUTDOWN_WAIT_S = 600.0

# Host-level tick outcomes, beside run_autocut's own (noop/ok/gated/...).
OUTCOME_DISABLED = "disabled"
OUTCOME_REFUSED = "refused"

HOMEBREW_BIN = "/opt/homebrew/bin"
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

# What the subprocess env keeps from the daemon's own: identity/locale only.
# Everything else (notably SNOWLINE_*_DATABASE_URL — the LIVE databases) is
# dropped, matching what the launchd timer's plist gives a cut.
_INHERITED_ENV = (
    "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "__CF_USER_TEXT_ENCODING", "SSH_AUTH_SOCK",
)

Run = Callable[["TickSettings"], object]
Sleep = Callable[[float], Awaitable[None]]


def host_home() -> Path:
    """The home whose ~/.config/snowline switches and app-support report the
    host uses. `SNOWLINE_AUTOCUT_HOME` overrides (tests)."""
    raw = os.environ.get("SNOWLINE_AUTOCUT_HOME")
    return Path(raw) if raw else Path.home()


def source_root() -> Path | None:
    """The platform checkout this package runs from, or None when it is not
    source-run (an installed wheel): <root>/src/snowline_platform/release/."""
    root = Path(__file__).resolve().parents[3]
    if (root / ".git").exists() and (root / "release" / "components.json").is_file():
        return root
    return None


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Pure-ish decisions (each unit-tested)
# --------------------------------------------------------------------------


def switch_state(home: Path) -> tuple[bool, str]:
    """(enabled, why). The kill switch wins over the enable marker."""
    off = a.disable_marker_path(home)
    if off.exists():
        return False, f"kill switch {off} exists"
    on = a.enable_marker_path(home)
    if not on.exists():
        return False, f"no enable marker ({on})"
    return True, f"enabled ({on})"


def load_env_file(home: Path) -> dict[str, str]:
    """~/.config/snowline/autocut.env as KEY=VALUE (optional `export`, quotes,
    comments) — parsed, never executed. Missing file -> {}."""
    from snowline_platform.stack import model as stack_model

    try:
        text = a.env_file_path(home).read_text()
    except FileNotFoundError:
        return {}
    return stack_model.parse_env_exports(text)


def subprocess_env(
    environ: Mapping[str, str],
    file_env: Mapping[str, str],
    *,
    platform_url: str,
    home: Path,
    venv_bin: Path | None = None,
) -> dict[str, str]:
    """The env every cut subprocess starts from (Runner.base_env).

    PATH: ~/.local/bin and Homebrew first (uv, gh, node/npm — the launchd
    timer's PATH), then the daemon's own PATH, the system dirs, and the
    platform venv's bin LAST (so `snowline` resolves without the platform
    venv's python shadowing the one `uv run` picks in each checkout).
    autocut.env wins over everything but PATH, which it may only prefix."""
    env = {k: environ[k] for k in _INHERITED_ENV if environ.get(k)}
    env["HOME"] = str(home)
    env["SNOWLINE_PLATFORM_URL"] = platform_url
    overlay = dict(file_env)
    file_path = overlay.pop("PATH", "")
    env.update(overlay)
    venv = venv_bin if venv_bin is not None else Path(sys.prefix) / "bin"
    parts = (
        file_path.split(":")
        + [str(home / ".local" / "bin"), HOMEBREW_BIN]
        + environ.get("PATH", "").split(":")
        + list(SYSTEM_PATH)
        + [str(venv)]
    )
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    env["PATH"] = ":".join(seen)
    return env


def _host_port(url: str) -> tuple[str, int] | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.scheme not in ("http", "https"):
        return None
    return parts.hostname.lower(), port or (443 if parts.scheme == "https" else 80)


def is_local_address(host: str) -> bool:
    """True when `host` resolves only to addresses this machine can bind —
    i.e. it names this machine (loopback or one of its own interfaces)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for family, _type, _proto, _canon, sockaddr in infos:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.bind((sockaddr[0], 0))
        except OSError:
            return False
    return True


def refusal(
    home: Path,
    platform_url: str | None,
    *,
    self_url: str,
    root: Path | None,
    is_local: Callable[[str], bool] = is_local_address,
) -> str | None:
    """Why this platform must not cut, or None. Not configurable."""
    from snowline_platform.stack import model as stack_model

    stack_json = stack_model.stack_config_path(home)
    if stack_json.exists():
        try:
            cfg = stack_model.load_stack_config(stack_json.read_text())
        except Exception as exc:  # noqa: BLE001 — unreadable means "can't prove hub"
            return f"{stack_json} is unreadable ({exc}) — cannot tell hub from spoke"
        if cfg is not None and cfg.role == "spoke":
            return (
                f"{stack_json} declares role=spoke — a packaged spoke never cuts "
                "trains (decision 54447516)"
            )
    if root is None:
        return (
            "the platform is not running from a git checkout (no .git / "
            "release/components.json beside the package) — the hub is source-run"
        )
    if not platform_url:
        return "SNOWLINE_PLATFORM_URL is unset (neither autocut.env nor the platform env)"
    target = _host_port(platform_url)
    own = _host_port(self_url)
    if target is None:
        return f"SNOWLINE_PLATFORM_URL {platform_url!r} is not an http(s) URL"
    if own is None or target != own:
        return (
            f"SNOWLINE_PLATFORM_URL {platform_url} is not this platform's own "
            f"address ({self_url}) — the gates must judge the hub that cuts"
        )
    if not is_local(target[0]):
        return f"SNOWLINE_PLATFORM_URL host {target[0]} is not an address of this machine"
    return None


# --------------------------------------------------------------------------
# The tick + loop
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TickSettings:
    home: Path
    root: Path
    platform_url: str
    env: dict[str, str]


def run_once(settings: TickSettings) -> a.AutocutReport:
    """Production `run`: exactly what `snowline release autocut` does, from
    the checkout this platform runs from, with the clean env. Blocking."""
    from . import model as m
    from . import runner as r

    def report(line: str) -> None:
        for part in str(line).splitlines() or [""]:
            log.info("autocut: %s", part)

    config = m.load_config(settings.root / "release" / "components.json")
    checkouts = {comp.name: m.resolve_checkout(config, comp, {}) for comp in config.components}
    runner = r.Runner(report=report, base_env=settings.env)
    return a.run_autocut(
        config,
        checkouts=checkouts,
        home=settings.home,
        runner=runner,
        platform_url=settings.platform_url,
        report=report,
    )


@dataclass
class HostState:
    """Live view on `app.state.autocut_host` (surfaced in GET /standby)."""

    alive: bool = False
    enabled: bool = False
    running: bool = False
    last_tick_at: str | None = None
    last_outcome: str | None = None
    last_detail: str | None = None
    next_tick_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "alive": self.alive,
            "enabled": self.enabled,
            "running": self.running,
            "last_tick_at": self.last_tick_at,
            "last_outcome": self.last_outcome,
            "last_detail": self.last_detail,
            "next_tick_at": self.next_tick_at,
        }


class AutocutHost:
    def __init__(
        self,
        *,
        home: Path | None = None,
        state: HostState | None = None,
        run: Run = run_once,
        self_url: Callable[[], str] | None = None,
        root: Path | None | object = ...,
        is_local: Callable[[str], bool] = is_local_address,
        environ: Mapping[str, str] | None = None,
        shutdown_wait_s: float = DEFAULT_SHUTDOWN_WAIT_S,
        now: Callable[[], datetime] = _utcnow,
        venv_bin: Path | None = None,
    ):
        from snowline_platform import config

        self.home = home if home is not None else host_home()
        self.state = state or HostState()
        self.run = run
        self.self_url = self_url or config.platform_self_url
        self.root = source_root() if root is ... else root
        self.is_local = is_local
        self.environ = environ
        self.shutdown_wait_s = shutdown_wait_s
        self.now = now
        self.venv_bin = venv_bin
        self._announced: tuple[str, str] | None = None

    # -- logging once per state change --
    def _announce(self, kind: str, why: str, level: int) -> None:
        key = (kind, why)
        if key == self._announced:
            return
        self._announced = key
        if kind == "enabled":
            log.log(level, "autocut host: %s — cutting on ticks", why)
        else:
            log.log(level, "autocut host %s: %s", "idle" if kind == "disabled" else "REFUSED", why)

    def announce_startup(self) -> None:
        enabled, why = switch_state(self.home)
        self.state.enabled = enabled
        self._announce("enabled" if enabled else "disabled", why, logging.INFO)

    def prepare(self) -> tuple[TickSettings | None, str | None]:
        environ = self.environ if self.environ is not None else os.environ
        file_env = load_env_file(self.home)
        platform_url = file_env.get("SNOWLINE_PLATFORM_URL") or environ.get("SNOWLINE_PLATFORM_URL")
        why = refusal(
            self.home, platform_url, self_url=self.self_url(), root=self.root,
            is_local=self.is_local,
        )
        if why:
            return None, why
        assert platform_url is not None and self.root is not None
        url = platform_url.rstrip("/")
        env = subprocess_env(
            environ, file_env, platform_url=url, home=self.home, venv_bin=self.venv_bin
        )
        return TickSettings(home=self.home, root=self.root, platform_url=url, env=env), None

    async def tick(self) -> str:
        """One tick; returns its outcome. Never raises (but cancellation)."""
        state = self.state
        state.last_tick_at = _iso(self.now())
        try:
            enabled, why = switch_state(self.home)
            state.enabled = enabled
            if not enabled:
                self._announce("disabled", why, logging.INFO)
                return self._record(OUTCOME_DISABLED, why)
            settings, refused = self.prepare()
            if refused:
                self._announce("refused", refused, logging.WARNING)
                return self._record(OUTCOME_REFUSED, refused)
            self._announce("enabled", why, logging.INFO)
            state.running = True
            try:
                result = await self._run_in_thread(settings)
            finally:
                state.running = False
            outcome = str(getattr(result, "outcome", result))
            detail = getattr(result, "detail", None)
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:  # the loop must never die (logged below)
            log.exception("autocut host tick raised")
            outcome, detail = a.OUTCOME_FAILED, f"unexpected {type(exc).__name__}: {exc}"
        log.info("autocut host tick: %s%s", outcome, f" — {detail}" if detail else "")
        return self._record(outcome, detail)

    def _record(self, outcome: str, detail: str | None) -> str:
        self.state.last_outcome = outcome
        self.state.last_detail = detail
        return outcome

    async def _run_in_thread(self, settings: TickSettings) -> object:
        finished = threading.Event()

        def call() -> object:
            try:
                return self.run(settings)
            finally:
                finished.set()

        try:
            return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
        except anyio.get_cancelled_exc_class():
            # Shutdown mid-cut: killing it could leave a train half-published
            # (tags pushed, releases missing). Wait — bounded — for it.
            if not finished.is_set():
                log.warning(
                    "autocut host: platform shutting down DURING a cut — waiting "
                    "up to %.0fs for it to finish before exiting",
                    self.shutdown_wait_s,
                )
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(finished.wait, self.shutdown_wait_s)
                if finished.is_set():
                    log.info("autocut host: the in-flight cut finished; shutdown continues")
                else:
                    log.error(
                        "autocut host: cut still running after %.0fs — abandoning it "
                        "to shutdown (check autocut-report.json and the train's tags)",
                        self.shutdown_wait_s,
                    )
            raise

    async def loop(
        self,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        startup_delay_s: float = DEFAULT_STARTUP_DELAY_S,
        sleep: Sleep = anyio.sleep,
    ) -> None:
        state = self.state
        state.alive = True
        try:
            try:
                self.announce_startup()
            except Exception:
                log.exception("autocut host: startup check raised")
            state.next_tick_at = _iso(self.now() + timedelta(seconds=startup_delay_s))
            await sleep(startup_delay_s)
            while True:
                await self.tick()
                state.next_tick_at = _iso(self.now() + timedelta(seconds=interval_s))
                await sleep(interval_s)
        finally:
            state.alive = False
            state.running = False
            state.next_tick_at = None


async def autocut_host_loop(
    app=None,
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    startup_delay_s: float = DEFAULT_STARTUP_DELAY_S,
    sleep: Sleep = anyio.sleep,
    **host_kwargs,
) -> None:
    """The platform lifespan's entry point: builds the host, exposes its
    state on `app.state.autocut_host`, and runs the loop until cancelled."""
    host = AutocutHost(**host_kwargs)
    if app is not None:
        app.state.autocut_host = host.state
    await host.loop(interval_s=interval_s, startup_delay_s=startup_delay_s, sleep=sleep)
