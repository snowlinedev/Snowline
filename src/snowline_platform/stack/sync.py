"""`snowline stack sync` orchestration (macOS distribution spec §5/§6, item
b70b0359 / issue #203).

Reuses the `release/runner.py` seam wholesale — the SAME `Runner` class, the
SAME `read()` (non-mutating, always executes) vs `run()` (mutating, no-op
under `--dry-run`) split. `model.py` makes every decision; everything here is
the side effect that decision implies. Two things are NOT run through
`Runner` because they are not subprocess calls: the atomic `current` symlink
swap (`os.replace`, spec §5 step 2 — plain `ln -sfn` leaves a no-`current`
window) and reading/writing the operator's own files (stack.json, *.env,
*.plist) — those take their own explicit `dry_run` guard instead.

`--dry-run` never gets past the planning branch below: no download, no venv,
no swap, no env/plist write, no createdb, no kickstart. This mirrors
`release.cutter.cut()`'s own dry-run short-circuit.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timezone
from pathlib import Path

from ..release import model as relm
from ..release import runner as relr
from ..release.runner import CommandError, Runner
from . import model as m

Report = Callable[[str], None]
PromptFn = Callable[[str], str]

PLATFORM_REPO = "snowlinedev/Snowline"
# Canonical tuple lives in model (derived from SERVICE_PORTS — #212 review);
# this alias keeps every existing `s.SERVICE_ORDER` reference working.
SERVICE_ORDER = m.SERVICE_ORDER
DEFAULT_HEALTH_URL = "http://127.0.0.1:8848/health"
# The failover front's own health (issue #260) — served by the front itself,
# never proxied, so it answers whether or not the hub is up.
FRONT_HEALTH_URL = m.front_health_url()

_TEMPLATES_DIR = Path(__file__).with_name("templates") / "env"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_optional(path: Path) -> str | None:
    return path.read_text() if path.exists() else None


# --------------------------------------------------------------------------
# Local state (no subprocess).
# --------------------------------------------------------------------------


def read_stack_config(home: Path) -> m.StackConfig | None:
    return m.load_stack_config(_read_optional(m.stack_config_path(home)))


def read_existing_env_texts(home: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for service in SERVICE_ORDER:
        text = _read_optional(m.env_file_path(home, service))
        if text is not None:
            out[service] = text
    return out


def read_current_train(home: Path, service: str) -> str | None:
    link = m.service_current_link(home, service)
    try:
        target = os.readlink(link)
    except OSError:
        return None
    return Path(target).name


def tty_prompt(message: str) -> str:
    """A prompt that survives `curl … | sh` (#210 review): there, stdin is the
    exhausted script pipe, so `input()` raises EOFError before the flagship
    one-command install ever writes stack.json. Fall back to the controlling
    terminal; refuse cleanly when there is none."""
    if sys.stdin.isatty():
        return input(message)
    try:
        with open("/dev/tty") as tty:
            sys.stderr.write(message)
            sys.stderr.flush()
            line = tty.readline()
    except OSError as exc:
        raise m.StackError(
            "stack.json setup needs an interactive terminal (stdin is not a "
            "tty and /dev/tty is unavailable) — run `snowline stack sync` "
            "from a terminal once"
        ) from exc
    if not line:
        raise m.StackError("stack.json setup: no input received from the terminal")
    return line.strip()


def check_not_source_hub(
    home: Path, *, plist_dirs: Sequence[Path] | None = None
) -> None:
    """Refuse to install the packaged stack on a machine already running
    Snowline services sync does not manage — i.e. the source-run hub, whose
    posture lives in launchd plists, not in the `<service>.env` filenames the
    env guard reads (#210 review: without this, `curl … | sh` on the mini
    would bootstrap a SECOND service set onto the hub's own ports). A
    stack-managed spoke always has stack.json, so its own plists don't trip
    this.

    The hub's plists live either in ~/Library/LaunchAgents (login-time) or,
    once moved by ops/hub/install-daemons.sh (#239), in the system
    /Library/LaunchDaemons — both are checked. `plist_dirs` is injectable so
    tests never read the real system directory."""
    if m.stack_config_path(home).exists():
        return
    if plist_dirs is None:
        plist_dirs = [m.launch_agents_dir(home), m.SYSTEM_LAUNCH_DAEMONS_DIR]
    existing = [
        m.plist_label(svc)
        for svc in SERVICE_ORDER
        if any((d / f"{m.plist_label(svc)}.plist").exists() for d in plist_dirs)
    ]
    if existing:
        raise m.StackError(
            "this machine already runs Snowline services that stack sync does "
            f"not manage ({', '.join(existing)}) with no stack.json — that is "
            "the source-run hub posture, and sync is SPOKE-ONLY (decision "
            "54447516); it will not stand up a second service set here"
        )


AUTO_NEEDS_STACK_JSON = (
    "`stack sync --auto` needs ~/.config/snowline/stack.json (written by "
    "bootstrap-spoke / setup); --auto never prompts; nothing was changed"
)


class SyncLock:
    """Non-blocking flock on sync.lock under Application Support (mirrors
    autocut's): held for the whole run, dropped by the kernel if the process
    dies. A timer fire during a manual sync (or vice versa) loses the race
    and exits 0 `locked`."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def _no_prompt(message: str) -> str:
    raise m.StackError(f"{AUTO_NEEDS_STACK_JSON} (would have asked: {message.strip()})")


def ensure_stack_config(
    home: Path,
    *,
    existing: m.StackConfig | None,
    auto: bool,
    dry_run: bool,
    prompt: PromptFn | None,
) -> m.StackConfig:
    if existing is not None:
        return existing
    if auto:
        raise m.StackError(
            AUTO_NEEDS_STACK_JSON
        )
    if prompt is None:
        raise m.StackError("stack.json is missing and no prompt function was supplied")
    instance_id = prompt("this instance's name (SNOWLINE_INSTANCE_ID, e.g. 'roam'): ").strip()
    primary = prompt(
        "the primary's tailnet address (e.g. mini.tailnet-name.ts.net): "
    ).strip()
    if not instance_id or not primary:
        raise m.StackError("stack.json setup needs a non-empty instance id and primary address")
    cfg = m.StackConfig(role="spoke", instance_id=instance_id, primary_tailnet_address=primary)
    if not dry_run:
        path = m.stack_config_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cfg.to_json())
    return cfg


# --------------------------------------------------------------------------
# gh preflight + train resolution.
# --------------------------------------------------------------------------


def preflight_gh_auth(runner: Runner) -> None:
    """Spec risk #4: checked BEFORE any symlink is touched."""
    if not relr.gh_auth_ok(runner):
        raise m.StackError(
            "gh is not authenticated (`gh auth status`) — checked before "
            "touching any symlink: the private pm wheel fetch needs it, and "
            "a mid-sync auth expiry would otherwise leave a half-sync"
        )


def latest_platform_tag(runner: Runner) -> str:
    out = runner.read(
        ["gh", "release", "view", "--repo", PLATFORM_REPO, "--json", "tagName", "--jq", ".tagName"]
    )
    tag = out.strip()
    if not tag:
        raise m.StackError(f"could not resolve the latest release tag on {PLATFORM_REPO}")
    return tag


# --------------------------------------------------------------------------
# Asset download.
# --------------------------------------------------------------------------


def download_dir(home: Path, train: str, repo: str, tag: str) -> Path:
    safe_repo = repo.replace("/", "__")
    return m.app_support_dir(home) / "downloads" / train / safe_repo / tag


def fetch_train_manifest(runner: Runner, version: str, *, into: Path) -> m.TrainManifest:
    into.mkdir(parents=True, exist_ok=True)
    runner.run(
        ["gh", "release", "download", version, "--repo", PLATFORM_REPO,
         "--pattern", "train.json", "--dir", str(into), "--clobber"]
    )
    manifest_path = into / "train.json"
    if not manifest_path.exists():
        raise m.StackError(f"train.json was not downloaded to {manifest_path}")
    return m.parse_train_manifest(json.loads(manifest_path.read_text()))


def unique_repo_tags(manifest: m.TrainManifest) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    for svc in manifest.services:
        pair = (svc.repo, svc.tag)
        if pair not in seen:
            seen.append(pair)
    return seen


def fetch_component_assets(runner: Runner, *, repo: str, tag: str, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    runner.run(
        ["gh", "release", "download", tag, "--repo", repo, "--dir", str(into), "--clobber"]
    )


def wheel_path(home: Path, train: str, entry: m.ServiceEntry) -> Path:
    return download_dir(home, train, entry.repo, entry.tag) / entry.wheel


def requirements_path(home: Path, train: str, entry: m.ServiceEntry) -> Path:
    return download_dir(home, train, entry.repo, entry.tag) / relm.requirements_filename(entry.service)


def dashboard_tarball_path(home: Path, train: str, platform_entry: m.ServiceEntry) -> Path:
    return download_dir(home, train, platform_entry.repo, platform_entry.tag) / relm.dashboard_tarball_name(
        platform_entry.tag
    )


# --------------------------------------------------------------------------
# Venv build + install (spec §5 step 2, risk #3, risk #6's install-time
# consequence: wheels by FILE PATH, never by name).
# --------------------------------------------------------------------------


def ensure_interpreter(runner: Runner) -> None:
    runner.run(["uv", "python", "install", "3.12"])


def note_stale_venv(home: Path, service: str, venv_dir: Path, *, report: Report = print) -> bool:
    """A target venv dir that already exists and is not what `current` points
    at is stale (33f5b3da: a failed attempt's leftovers made `uv venv` refuse
    "A virtual environment already exists" on every retry). `uv venv --clear`
    replaces it; this logs the fact and refuses to touch the live `current`
    target."""
    if not (venv_dir.exists() or venv_dir.is_symlink()):
        return False
    link = m.service_current_link(home, service)
    try:
        live = link.resolve() == venv_dir.resolve()
    except OSError:
        live = False
    if live or running_under(venv_dir):
        raise m.StackError(
            f"{venv_dir} is the live `current` venv for {service} (or the venv this CLI runs from) — refusing to rebuild it in place"
        )
    report(f"replacing stale {service} venv {venv_dir} (uv venv --clear)")
    return True


REVERTED_MARK = ".reverted-"


def set_aside_reverted_venv(home: Path, service: str, train: str) -> Path | None:
    """Move a reverted train's venv to `<train>.reverted-<UTC stamp>` (kept
    for autopsy; `gc_service_trains` keeps the newest per train). Never
    deletes. Returns the new path, or None when there was nothing to move."""
    src = m.service_venv_dir(home, service, train)
    if not src.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = src.with_name(f"{train}{REVERTED_MARK}{stamp}")
    n = 1
    while dest.exists():
        n += 1
        dest = src.with_name(f"{train}{REVERTED_MARK}{stamp}-{n}")
    os.replace(src, dest)
    return dest


def build_service_venv(
    runner: Runner,
    *,
    home: Path,
    train: str,
    manifest: m.TrainManifest,
    service: str,
    find_links: list[Path],
    report: Report = print,
) -> Path:
    entry = manifest.service(service)
    sdk_entry = manifest.sdk()
    venv_dir = m.service_venv_dir(home, service, train)
    note_stale_venv(home, service, venv_dir, report=report)
    ensure_interpreter(runner)
    runner.run(["uv", "venv", "--python", "3.12", "--clear", str(venv_dir)])
    python = venv_dir / "bin" / "python"
    install = [
        "uv", "pip", "install", "--python", str(python),
        str(wheel_path(home, train, entry)),
        str(wheel_path(home, train, sdk_entry)),
    ]
    for link in find_links:
        install += ["--find-links", str(link)]
    install += ["-r", str(requirements_path(home, train, entry))]
    runner.run(install)
    # Risk #3: verify the uv-managed interpreter is actually there before ANY
    # symlink swap happens for ANY service (sync builds+verifies every
    # changed service before it swaps a single one — see `sync()` below).
    runner.read([str(python), "--version"])
    return venv_dir


def unpack_dashboard(runner: Runner, *, tarball: Path, dest: Path) -> None:
    # The cutter packs `tar -czf … -C <dashboard> dist` — a TOP-LEVEL `dist/`
    # entry (that is the shape v0.1.0 shipped with). Strip it so `dest` itself
    # contains index.html; without this, SNOWLINE_DASHBOARD_DIST pointed one
    # level too high and every packaged install's /ui 404'd (#210 review).
    dest.mkdir(parents=True, exist_ok=True)
    runner.run(
        ["tar", "-xzf", str(tarball), "-C", str(dest), "--strip-components", "1"]
    )


def swap_dashboard_current(home: Path, train: str) -> None:
    """Repoint the STABLE dashboard path at this train's unpacked dist.

    The env files bake `SNOWLINE_DASHBOARD_DIST=<…>/dashboard/current` — a
    path that never changes — because env files are operator-owned and sync
    never clobbers them: a train-versioned value would freeze the UI at the
    install-time train forever and read as perpetual env drift (#210
    review). The symlink is the train-versioned part, and sync owns it."""
    link = m.dashboard_root(home) / "current"
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(link.name + ".tmp-new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(m.dashboard_dir(home, train))
    os.replace(tmp, link)


# --------------------------------------------------------------------------
# Atomic symlink swap (spec §5 step 2 — os.replace, never `ln -sfn`) + GC.
# --------------------------------------------------------------------------


def swap_current(home: Path, service: str, target_dir: Path) -> None:
    link = m.service_current_link(home, service)
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(link.name + ".tmp-new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target_dir)
    os.replace(tmp, link)  # atomic rename(2) — no no-`current` window


def prune_reverted_venvs(home: Path, service: str) -> list[str]:
    """Keep only the newest `<train>.reverted-<stamp>` dir per train."""
    root = m.service_dir(home, service)
    if not root.exists():
        return []
    reverted = [p.name for p in root.iterdir() if p.is_dir() and REVERTED_MARK in p.name]
    newest: dict[str, str] = {}
    for n in reverted:
        base, stamp = n.split(REVERTED_MARK, 1)
        if base not in newest or stamp > newest[base].split(REVERTED_MARK, 1)[1]:
            newest[base] = n
    doomed = sorted(n for n in reverted if n not in newest.values())
    for name in doomed:
        shutil.rmtree(root / name, ignore_errors=True)
    return doomed


def gc_service_trains(home: Path, service: str, *, keep: set[str]) -> list[str]:
    root = m.service_dir(home, service)
    if not root.exists():
        return []
    installed = [
        p.name for p in root.iterdir()
        if p.is_dir() and p.name != "current" and REVERTED_MARK not in p.name
    ]
    doomed = m.trains_to_gc(installed, keep=list(keep))
    for name in doomed:
        shutil.rmtree(root / name, ignore_errors=True)
    return doomed + prune_reverted_venvs(home, service)


# --------------------------------------------------------------------------
# Migration-head probing.
# --------------------------------------------------------------------------


def probe_heads(runner: Runner, python: Path, service: str) -> list[str]:
    out = runner.read(m.head_probe_argv(python, service))
    return m.parse_heads(out)


# --------------------------------------------------------------------------
# Env + plist rendering.
# --------------------------------------------------------------------------


def load_env_template(service: str) -> str:
    text = (_TEMPLATES_DIR / f"{service}.env.tmpl").read_text()
    if service == "platform":
        text += load_platform_front_block()
    return text


# The spoke platform's front-health key (issue #296): a self-contained tail of
# platform.env.tmpl, kept as its own file so it can be APPENDED to an existing
# operator-owned platform.env without rewriting anything above it.
_FRONT_KEY = "SNOWLINE_FRONT_HEALTH_URL"
_FRONT_KEY_RE = re.compile(rf"^\s*(?:export\s+)?{_FRONT_KEY}=", re.MULTILINE)


def load_platform_front_block() -> str:
    return (_TEMPLATES_DIR / "platform.front.env.frag").read_text()


def ensure_platform_front_key(
    home: Path, *, variables: Mapping[str, str], front_installed: bool, dry_run: bool
) -> bool:
    """Template `SNOWLINE_FRONT_HEALTH_URL` into an EXISTING spoke
    `platform.env` that lacks it (issue #296), so the standby readiness panel
    shows the front row without a manual edit. Same ownership rule as every
    env file: the key is only ever ADDED — an existing assignment (any value) is
    never touched, and a missing file is left to `apply_env_file`, whose
    template already carries the key.
    Spoke-only by construction (sync refuses the hub) and only when the
    failover front is installed. Returns whether the key was (or, on a dry
    run, would be) appended."""
    if not front_installed:
        return False
    path = m.env_file_path(home, "platform")
    existing = _read_optional(path)
    if existing is None or _FRONT_KEY_RE.search(existing):
        return False
    if not dry_run:
        block = m.render_env(load_platform_front_block(), variables)
        sep = "" if existing.endswith("\n") else "\n"
        # Append, never truncate-then-write: the file is operator-owned and
        # carries DB URLs — a crash between truncate and write would empty it.
        # "a" keeps mode and ownership.
        with path.open("a") as fh:
            fh.write(sep + block)
    return True


def env_variables(cfg: m.StackConfig, *, dashboard_dist: str) -> dict[str, str]:
    platform_port = m.SERVICE_PORTS["platform"]
    return {
        "hub_url": m.front_hub_url(cfg),
        "spoke_url": f"http://127.0.0.1:{platform_port}",
        "front_port": str(m.FRONT_PORT),
        "front_health_url": m.front_health_url(),
        "instance_id": cfg.instance_id,
        "trusted_cidrs": m.TRUSTED_CIDRS,
        "primary_tailnet_address": cfg.primary_tailnet_address,
        "platform_port": str(m.SERVICE_PORTS["platform"]),
        "governance_port": str(m.SERVICE_PORTS["governance"]),
        "memory_port": str(m.SERVICE_PORTS["memory"]),
        "pm_port": str(m.SERVICE_PORTS["pm"]),
        "dashboard_dist": dashboard_dist,
    }


def apply_env_file(
    home: Path, service: str, *, variables: Mapping[str, str], dry_run: bool
) -> m.EnvFileResult:
    rendered = m.render_env(load_env_template(service), variables)
    path = m.env_file_path(home, service)
    existing = _read_optional(path)
    result = m.plan_env_file(service=service, path=path, existing=existing, rendered=rendered)
    if result.action == "written" and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
    return result


def apply_plist(home: Path, service: str, *, dry_run: bool) -> bool:
    """Plists are entirely sync-owned (unlike env files) — regenerated every
    run, written only when content actually changed. They SOURCE the env file
    at exec time (never bake its values, #210 review), so their content no
    longer depends on the env file's text at all. Returns whether it
    changed."""
    rendered = m.render_plist(
        service=service,
        venv_current=m.service_current_link(home, service),
        port=m.SERVICE_PORTS[service],
        env_file=m.env_file_path(home, service),
        logs=m.log_dir(home),
    )
    path = m.plist_path(home, service)
    existing = _read_optional(path)
    if existing == rendered:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
    return True


def apply_front_plist(home: Path, *, dry_run: bool) -> bool:
    """The failover front's plist (issue #260) — same ownership rules as the
    services' (sync-owned, rewritten only on change). Returns whether it
    changed."""
    rendered = m.render_front_plist(
        platform_venv_current=m.service_current_link(home, m.FRONT_VENV_SERVICE),
        port=m.FRONT_PORT,
        env_file=m.env_file_path(home, m.FRONT_SERVICE),
        logs=m.log_dir(home),
    )
    path = m.plist_path(home, m.FRONT_SERVICE)
    existing = _read_optional(path)
    if existing == rendered:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
    return True


def apply_autoupgrade_plist(home: Path, *, dry_run: bool) -> str:
    """The hourly auto-upgrade timer's plist (#208): sync-owned like the
    front's. Returns "installed" (no file before), "updated" (content
    changed) or "unchanged"."""
    rendered = m.render_autoupgrade_plist(
        home=home,
        platform_venv_current=m.service_current_link(home, m.FRONT_VENV_SERVICE),
        logs=m.log_dir(home),
    )
    path = m.plist_path(home, m.AUTOUPGRADE_UNIT)
    existing = _read_optional(path)
    if existing == rendered:
        return "unchanged"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        m.log_dir(home).mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
    return "installed" if existing is None else "updated"


def is_timer_run() -> bool:
    """True when this process IS the auto-upgrade timer's launchd job."""
    return os.environ.get("XPC_SERVICE_NAME") == m.plist_label(m.AUTOUPGRADE_UNIT)


def install_autoupgrade_timer(
    runner: Runner, home: Path, *, dry_run: bool, timer_run: bool | None = None
) -> str:
    """Render the timer plist and load it. NEVER kickstarted (RunAtLoad is
    false — loading must not start a sync inside a sync). launchctl calls are
    best-effort (check=False): a failed load must not fail an upgrade.

    Returns "installed" | "updated" | "reloaded" | "healed" | "deferred" | "reload-failed" |
    "unchanged". When this very process IS the timer job, `bootout` would
    SIGTERM the running sync mid-upgrade, so a changed plist is only written
    and `autoupgrade.reload-pending` is touched ("deferred"); the next
    non-timer sync (or bootstrap-spoke) does the bootout+bootstrap and clears
    the marker. An unchanged plist that is not loaded (operator bootout) is
    re-bootstrapped."""
    action = apply_autoupgrade_plist(home, dry_run=dry_run)
    if dry_run:
        return action
    uid = os.getuid()
    label = m.plist_label(m.AUTOUPGRADE_UNIT)
    domain, target = f"gui/{uid}", m.gui_target(label, uid)
    plist = str(m.plist_path(home, m.AUTOUPGRADE_UNIT))
    marker = m.autoupgrade_reload_pending_path(home)
    if timer_run is None:
        timer_run = is_timer_run()
    if timer_run:
        if action == "unchanged":
            return action
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return "deferred"
    pending = marker.exists()
    if action == "installed":
        runner.run(["launchctl", "bootout", target], check=False)  # replace a stale loaded config
        runner.run(["launchctl", "bootstrap", domain, plist], check=False)
    elif action == "updated" or pending:
        runner.run(["launchctl", "bootout", target], check=False)
        # Clear the reload-pending marker only once the bootstrap really
        # succeeded: a failed bootstrap leaves the job unloaded, and the
        # marker keeps the next non-timer sync retrying (the heal path would
        # too, but the marker says WHY).
        try:
            runner.run(["launchctl", "bootstrap", domain, plist])
        except CommandError:
            return action if action != "unchanged" else "reload-failed"
        if action == "unchanged":
            action = "reloaded"
    else:
        try:
            runner.read(["launchctl", "print", target])
        except CommandError:
            runner.run(["launchctl", "bootstrap", domain, plist], check=False)
            action = "healed"
    if pending:
        marker.unlink(missing_ok=True)
    return action


def _timer_note(action: str, *, dry_run: bool) -> str | None:
    if action == "unchanged":
        return None
    label = m.plist_label(m.AUTOUPGRADE_UNIT)
    if action == "deferred":
        return "timer plist updated; reload on the next manual `stack sync` or login"
    if dry_run:
        return f"would {'install' if action == 'installed' else 'update'} the hourly auto-upgrade timer ({label})"
    if action == "healed":
        return f"re-loaded the hourly auto-upgrade timer ({label}), which was not loaded"
    if action == "reload-failed":
        return (
            f"the hourly auto-upgrade timer ({label}) was booted out but `launchctl "
            f"bootstrap` FAILED — it is not loaded; reload-pending kept, the next "
            f"manual `stack sync` retries (check the plist with plutil -lint)"
        )
    return f"{action} the hourly auto-upgrade timer ({label})"


# --------------------------------------------------------------------------
# Stack-managed units (#303): ONE reconcile pass, run by BOTH the full path
# and the no-op path, so no unit can ever be "installed only by an upgrade".
# A unit is everything sync owns that is NOT a train-versioned service venv
# (the front, the platform's front-health key, the auto-upgrade timer, …).
# A future unit is one more entry in `UNITS` — never a branch in a path.
# --------------------------------------------------------------------------

UNIT_PLATFORM_FRONT_KEY = "platform-front-key"


@dataclass(frozen=True)
class UnitContext:
    home: Path
    runner: Runner
    variables: Mapping[str, str]
    dry_run: bool
    # This process is the timer's own launchd job: a changed timer plist is
    # written but its reload deferred (a bootout would kill this very run).
    timer_run: bool
    # The platform venv was just swapped: the front runs from it, so it is
    # kicked even when its own files did not change.
    platform_swapped: bool


def _reconcile_platform_front_key(ctx: UnitContext) -> m.UnitAction:
    # Ordered BEFORE the front and (on the full path) before the services'
    # env drift pass, so a pre-#296 platform.env re-matches the template.
    # `front_installed=True`: the front is reconciled in this same pass.
    added = ensure_platform_front_key(
        ctx.home, variables=ctx.variables, front_installed=True, dry_run=ctx.dry_run
    )
    if not added:
        return m.UnitAction(name=UNIT_PLATFORM_FRONT_KEY, action="unchanged")
    note = (
        f"would add {_FRONT_KEY} to platform.env" if ctx.dry_run
        else f"added {_FRONT_KEY} to platform.env and restarted the platform"
    )
    return m.UnitAction(name=UNIT_PLATFORM_FRONT_KEY, action="added", kick="platform", note=note)


def _reconcile_front(ctx: UnitContext) -> m.UnitAction:
    """The failover front (#260): front.env (operator-owned, drift reported)
    + its plist (sync-owned); kicked when either was newly written or the
    platform venv it runs from was swapped."""
    env = apply_env_file(ctx.home, m.FRONT_SERVICE, variables=ctx.variables, dry_run=ctx.dry_run)
    plist_existed = m.plist_path(ctx.home, m.FRONT_SERVICE).exists()
    plist_changed = apply_front_plist(ctx.home, dry_run=ctx.dry_run)
    if not plist_existed:
        action = "installed"
    elif plist_changed or env.action == "written":
        action = "updated"
    else:
        action = "unchanged"
    note = None
    if action != "unchanged":
        verb = {"installed": "install", "updated": "update"}[action]
        note = (
            f"{'would ' + verb if ctx.dry_run else action} the failover front "
            f"({m.plist_label(m.FRONT_SERVICE)})"
        )
    return m.UnitAction(
        name=m.FRONT_SERVICE, action=action,
        kick=m.FRONT_SERVICE if action != "unchanged" or ctx.platform_swapped else None,
        drift=env.action == "drifted", note=note,
    )


def _reconcile_autoupgrade_timer(ctx: UnitContext) -> m.UnitAction:
    """The hourly auto-upgrade timer (#208): loaded, never kickstarted."""
    action = install_autoupgrade_timer(
        ctx.runner, ctx.home, dry_run=ctx.dry_run, timer_run=ctx.timer_run
    )
    return m.UnitAction(
        name=m.AUTOUPGRADE_UNIT, action=action, note=_timer_note(action, dry_run=ctx.dry_run)
    )


# Order is load-bearing only for the front-health key (see above).
UNITS: tuple[Callable[[UnitContext], m.UnitAction], ...] = (
    _reconcile_platform_front_key,
    _reconcile_front,
    _reconcile_autoupgrade_timer,
)


def kick_units(
    runner: Runner,
    home: Path,
    actions: Sequence[m.UnitAction],
    *,
    dry_run: bool,
    already_kicked: Mapping[str, bool] | None = None,
) -> list[m.UnitAction]:
    """Kickstart every service the reconciled units asked for, once each,
    in unit order. `already_kicked` = services this run already kickstarted
    (their result is reused, never a second kick)."""
    if dry_run:
        return list(actions)
    results: dict[str, bool] = dict(already_kicked or {})
    out: list[m.UnitAction] = []
    for action in actions:
        if action.kick is None:
            out.append(action)
            continue
        if action.kick not in results:
            results[action.kick] = kickstart_service(runner, home, action.kick)
        out.append(replace(action, kicked=results[action.kick]))
    return out


def reconcile_units(
    home: Path,
    cfg: m.StackConfig,
    runner: Runner,
    *,
    dry_run: bool,
    timer_run: bool,
    platform_swapped: bool = False,
    kick: bool = True,
) -> list[m.UnitAction]:
    """Bring every stack-managed unit to the state THIS train's code renders.
    Idempotent: an unchanged unit is not written, not kicked, and gets no
    launchctl call (the one exception is the timer's heal of an unloaded job
    on a non-timer run). `kick=False` leaves the kickstarts to the caller
    (`kick_units`) — the full path kicks units AFTER the services they run
    from."""
    ctx = UnitContext(
        home=home, runner=runner,
        variables=env_variables(cfg, dashboard_dist=str(m.dashboard_root(home) / "current")),
        dry_run=dry_run, timer_run=timer_run, platform_swapped=platform_swapped,
    )
    actions = [unit(ctx) for unit in UNITS]
    return kick_units(runner, home, actions, dry_run=dry_run) if kick else actions


def _unit_notes(units: Sequence[m.UnitAction]) -> list[str]:
    notes: list[str] = []
    for u in units:
        if u.name == UNIT_PLATFORM_FRONT_KEY and u.kicked is False:
            notes.append(FRONT_KEY_KICK_FAILED_DETAIL)
        elif u.note:
            notes.append(u.note)
    return notes


def _key_kick_failed(units: Sequence[m.UnitAction]) -> bool:
    return any(u.name == UNIT_PLATFORM_FRONT_KEY and u.kicked is False for u in units)


def _front_kicked(units: Sequence[m.UnitAction]) -> bool | None:
    return next((u.kicked for u in units if u.name == m.FRONT_SERVICE), None)


# --------------------------------------------------------------------------
# Re-exec the NEW train's CLI after a platform swap (#303).
# --------------------------------------------------------------------------


def new_cli_path(home: Path) -> Path:
    return m.service_current_link(home, "platform") / "bin" / "snowline"


def running_under(venv_dir: Path) -> bool:
    """Whether this interpreter runs from `venv_dir` (sys.prefix is the venv
    dir; sys.executable would resolve through to uv's shared interpreter)."""
    try:
        return Path(sys.prefix).resolve() == venv_dir.resolve()
    except OSError:
        return False


def reexec_argv(
    cli: Path, *, role: str, train: str, auto: bool, health_url: str, verbose: bool,
    resumed_from: str,
) -> list[str]:
    """The SAME run's flags, with the train pinned to the one just installed
    (so the new CLI cannot resolve a newer release mid-run) and
    `--resumed-from` stopping a second re-exec."""
    argv = [str(cli), "stack", "sync", "--role", role, "--train", train]
    if auto:
        argv.append("--auto")
    if health_url != DEFAULT_HEALTH_URL:
        argv += ["--health-url", health_url]
    if verbose:
        argv.append("-v")
    return argv + ["--resumed-from", resumed_from]


def write_handoff(home: Path, rpt: m.RunReport) -> None:
    path = m.sync_handoff_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rpt.to_json())


def take_handoff(home: Path) -> dict | None:
    """Read and remove the old process's partial report (its swap: services,
    env drift, start time). None when absent or unreadable."""
    path = m.sync_handoff_path(home)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    finally:
        path.unlink(missing_ok=True)
    return data if isinstance(data, dict) else None


def _handoff_services(data: Mapping[str, object]) -> tuple[m.ServiceChange, ...]:
    out = []
    for s in data.get("services") or ():
        if isinstance(s, Mapping):
            out.append(m.ServiceChange(
                service=str(s.get("service")), previous_train=s.get("previous_train"),
                target_train=str(s.get("target_train")), changed=bool(s.get("changed")),
                migration_crossed=bool(s.get("migration_crossed")), kickstarted=s.get("kickstarted"),
            ))
    return tuple(out)


# --------------------------------------------------------------------------
# createdb (spec §3 — sync's only DB job; alembic is boot-migrate's).
# --------------------------------------------------------------------------


def existing_databases(runner: Runner) -> set[str]:
    out = runner.read(["psql", "-Atqc", "SELECT datname FROM pg_database", "-d", "postgres"])
    return {line.strip() for line in out.splitlines() if line.strip()}


def ensure_databases(runner: Runner, report: Report) -> list[str]:
    existing = existing_databases(runner)
    created: list[str] = []
    for db in sorted(set(m.DB_NAME.values())):
        if db not in existing:
            runner.run(["createdb", db])
            created.append(db)
            report(f"  created database {db}")
    return created


# --------------------------------------------------------------------------
# launchd + health check.
# --------------------------------------------------------------------------


def kickstart_service(runner: Runner, home: Path, service: str) -> bool:
    label = m.plist_label(service)
    uid = os.getuid()
    plist = m.plist_path(home, service)
    # `bootstrap` is best-effort/idempotent — "already bootstrapped" is not a
    # failure, so it goes through `read()` (check=False) rather than raising;
    # `kickstart` is the one that must actually report success/failure.
    runner.read(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], check=False)
    try:
        runner.run(["launchctl", "kickstart", "-k", m.gui_target(label, uid)])
        return True
    except CommandError:
        return False


def check_health(
    runner: Runner,
    url: str = DEFAULT_HEALTH_URL,
    *,
    attempts: int = 30,
    delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll until healthy or the budget runs out. A single immediate curl
    right after `launchctl kickstart` races uvicorn's bind and boot-migrate —
    it declared every healthy upgrade unhealthy, auto-reverting good trains
    and paging on good fresh installs (#210 review)."""
    for i in range(attempts):
        try:
            runner.read(["curl", "-fsS", "--max-time", "5", url])
            return True
        except CommandError:
            if i + 1 < attempts:
                sleep(delay)
    return False


# Post-UPGRADE health budget (4f9dfc47): a cold boot with all four services
# boot-migrating at once outlasted the old 30 s poll and false-reverted a good
# train. A WALL-CLOCK budget in seconds (each poll's own cost counts, so a
# hanging gateway cannot stretch it). Override: SNOWLINE_SYNC_HEALTH_BUDGET_S.
UPGRADE_HEALTH_BUDGET_S = 120.0
HEALTH_BUDGET_ENV = "SNOWLINE_SYNC_HEALTH_BUDGET_S"
DIAG_LOG_TAIL_BYTES = 64 * 1024


def upgrade_health_budget_s() -> float:
    raw = os.environ.get(HEALTH_BUDGET_ENV, "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return UPGRADE_HEALTH_BUDGET_S


def service_health_urls(health_url: str = DEFAULT_HEALTH_URL) -> dict[str, str]:
    """Each service's own `/health`; the platform's is the gateway URL."""
    urls = {svc: f"http://127.0.0.1:{port}/health" for svc, port in m.SERVICE_PORTS.items()}
    urls["platform"] = health_url
    return urls


@dataclass
class ServiceProbe:
    """Per-service health observed during the budget."""
    url: str
    answered_at_poll: int | None = None  # 1-based; None = never answered
    last_error: str | None = None


def poll_upgrade_health(
    runner: Runner,
    health_url: str,
    *,
    budget_s: float | None = None,
    attempts: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    delay: float = 1.0,
) -> tuple[bool, dict[str, ServiceProbe], int]:
    """Poll the gateway until it answers, `budget_s` seconds of wall clock
    (poll time + sleeps) are spent, or `attempts` polls are made. While the
    gateway is not answering, each service's own /health is polled too so a
    failed upgrade can name WHICH service was slow. Only the gateway decides
    the result. Returns `(gateway_ok, probes, polls_made)`."""
    probes = {svc: ServiceProbe(url) for svc, url in service_health_urls(health_url).items()}
    elapsed = 0.0
    polls = 0
    while True:
        polls += 1
        t0 = time.monotonic()
        for probe in probes.values():
            if probe.answered_at_poll is not None:
                continue
            try:
                runner.read(["curl", "-fsS", "--max-time", "5", probe.url])
                probe.answered_at_poll = polls
            except CommandError as exc:
                probe.last_error = str(exc)[:200]
        elapsed += time.monotonic() - t0
        if probes["platform"].answered_at_poll is not None:
            return True, probes, polls
        if attempts is not None and polls >= attempts:
            return False, probes, polls
        elapsed += delay
        if budget_s is not None and elapsed >= budget_s:
            return False, probes, polls
        sleep(delay)


def stack_health(
    runner: Runner,
    health_url: str,
    *,
    budget_s: float | None = None,
    attempts: int | None = None,
    front_attempts: int = 30,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, bool | None, dict[str, ServiceProbe], int]:
    """`check_stack_health` with per-service probes: `(gateway_ok, front_ok,
    probes, polls)`; `front_ok` is None when the gateway failed."""
    gw_ok, probes, polls = poll_upgrade_health(
        runner, health_url, budget_s=budget_s, attempts=attempts, sleep=sleep
    )
    if not gw_ok:
        return False, None, probes, polls
    return True, check_health(runner, FRONT_HEALTH_URL, attempts=front_attempts, sleep=sleep), probes, polls


def _tail_lines(path: Path, n: int) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - DIAG_LOG_TAIL_BYTES))
        data = fh.read()
    return data.decode(errors="replace").splitlines()[-n:]


def capture_health_diagnostic(
    runner: Runner, home: Path, probes: Mapping[str, ServiceProbe], *,
    budget_s: float, polls: int, tail_lines: int = 40,
) -> dict:
    """What the report keeps when an upgrade is declared unhealthy: per
    service the err.log tail, launchd state and whether its /health answered."""
    uid = os.getuid()
    diag: dict = {"budget_s": budget_s, "polls": polls, "services": {}}
    for svc, probe in probes.items():
        entry: dict = {
            "health_url": probe.url,
            "answered": probe.answered_at_poll is not None,
            "answered_at_poll": probe.answered_at_poll,
            "last_error": probe.last_error,
        }
        try:
            entry["err_log_tail"] = _tail_lines(m.service_err_log_path(home, svc), tail_lines)
        except OSError as exc:
            entry["err_log_tail"] = [f"(unreadable: {exc})"]
        try:
            out = runner.read(["launchctl", "print", m.gui_target(m.plist_label(svc), uid)], check=False)
        except (CommandError, OSError) as exc:  # diagnostics must never break the revert
            out = f"(launchctl print failed: {exc})"
        keep = [ln.strip() for ln in out.splitlines()
                if re.match(r"\s*(state|pid|runs|last exit code|active count|program)\b", ln)]
        entry["launchctl"] = keep[:12] or out.splitlines()[:12] or ["(no output — not loaded?)"]
        diag["services"][svc] = entry
    diag["not_answering"] = [svc for svc, p in probes.items() if p.answered_at_poll is None]
    return diag


def check_stack_health(
    runner: Runner,
    health_url: str = DEFAULT_HEALTH_URL,
    *,
    front_url: str = FRONT_HEALTH_URL,
    attempts: int = 30,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, bool | None]:
    """`(gateway_ok, front_ok)` — the gateway's `/health` and the failover
    front's own `/front/health` (issue #260), kept SEPARATE: only the gateway
    decides a train's fate (ok / revert). A front can die for reasons no
    train caused — port 8850 taken (KeepAlive crash-loop), an operator edit
    to front.env failing validation — and coupling it to the revert decision
    would revert every later train forever. A dead front is REPORTED
    (`needs_attention` with its own detail), never a revert trigger.
    `front_ok` is None when the gateway already failed (not checked — no
    point spending another full poll budget)."""
    if not check_health(runner, health_url, attempts=attempts, sleep=sleep):
        return False, None
    return True, check_health(runner, front_url, attempts=attempts, sleep=sleep)


FRONT_DOWN_DETAIL = (
    "the gateway is healthy but the failover front is NOT answering "
    f"{FRONT_HEALTH_URL} — the MBP's Claude Code MCP entry "
    "(http://127.0.0.1:8850/mcp) has no endpoint. Check "
    "~/Library/Application Support/Snowline/logs/snowline-front.err.log "
    "(a bad ~/.config/snowline/front.env value, or port 8850 already "
    "taken: `lsof -nP -iTCP:8850 -sTCP:LISTEN`), then `launchctl kickstart "
    "-k gui/$(id -u)/dev.snowline.front`. Not a train problem — nothing was "
    "reverted for it."
)

FRONT_KEY_KICK_FAILED_DETAIL = (
    "SNOWLINE_FRONT_HEALTH_URL was added to platform.env but the platform "
    "kickstart FAILED, so the running platform does not have it; sync will not "
    "retry (the key is present now) — run "
    "`launchctl kickstart -k gui/$(id -u)/dev.snowline.platform` by hand and "
    "check the platform's err.log"
)


# --------------------------------------------------------------------------
# Run report persistence.
# --------------------------------------------------------------------------


def report_history_stamp(finished_at: str) -> str:
    """ISO timestamp -> filename-safe history stamp. Z-normalize BEFORE
    stripping colons: the other order turns '+00:00' into '+0000' first,
    leaving the Z replacement dead and a '+' in every filename (#212
    review — the bug existed here and was copy-pasted into the bootstrap
    report before this shared helper existed)."""
    return finished_at.replace("+00:00", "Z").replace(":", "")


def last_attempt_for_train(home: Path, train: str) -> dict | None:
    """Newest history report for `train` that records a real attempt —
    `skipped` / `locked` reports are ignored so a skip never hides the
    revert it is waiting out."""
    history_dir = m.report_history_dir(home)
    if not history_dir.is_dir():
        return None
    for path in sorted(history_dir.glob(f"*-{train}.json"), reverse=True):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("train") != train:
            continue
        # skipped / locked / noop-path reports are not attempts: they must
        # never hide the revert a skip is waiting out.
        if data.get("outcome") in ("skipped", "locked") or data.get("noop"):
            continue
        return data
    return None


def write_report(home: Path, report: m.RunReport, *, dry_run: bool) -> None:
    if dry_run:
        return
    path = m.report_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json())
    history_dir = m.report_history_dir(home)
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = report_history_stamp(report.finished_at)
    (history_dir / f"{stamp}-{report.train}.json").write_text(report.to_json())


# --------------------------------------------------------------------------
# The orchestrator.
# --------------------------------------------------------------------------


def run_sync(
    *,
    home: Path,
    role: str = "spoke",
    train: str | None = None,
    auto: bool = False,
    dry_run: bool = False,
    runner: Runner,
    prompt: PromptFn | None = None,
    health_url: str = DEFAULT_HEALTH_URL,
    health_sleep: Callable[[float], None] = time.sleep,
    report: Report = print,
    resumed_from: str | None = None,
    verbose: bool = False,
) -> m.RunReport:
    """Serialised by sync.lock (#208), acquired once the posture/hub checks
    pass (a sync on the hub leaves no lock file): a concurrent run (the hourly
    timer during a manual sync, or the reverse) exits 0 `locked` with a
    one-line report and touches nothing. Dry runs skip the lock.

    After a successful forward platform swap, a process not running from the
    new platform venv re-execs the NEW train's CLI (`--resumed-from`, #303)
    so the run finishes under the new code's unit list; that resumed run
    takes the no-op path, reconciles every unit, re-checks health and writes
    the run's single report. `resumed_from` is set on that resumed run."""
    started_at = _now_iso()
    resumed = resumed_from is not None or os.environ.get(m.SYNC_RESUMED_ENV) == "1"
    timer_run = is_timer_run()
    resolved_train = train or "unknown"
    instance_id = "unknown"
    lock: SyncLock | None = None
    try:
        existing_cfg = read_stack_config(home)
        m.check_spoke_posture(requested_role=role, existing=existing_cfg)
        m.check_no_primary_env(read_existing_env_texts(home))
        check_not_source_hub(home)
        if not dry_run:
            lock = SyncLock(m.sync_lock_path(home))
            if not lock.acquire():
                lock = None
                detail = "another stack sync is running (sync.lock held) — skipped"
                if resumed_from is not None:
                    # The resumed pass lost the lock race (an hourly fire won):
                    # that run reconciles the units itself; the swap record is
                    # discarded here so it can never be mis-applied later.
                    handoff = take_handoff(home)
                    swapped = ", ".join(
                        c.get("service", "?") for c in (handoff or {}).get("services", [])
                        if isinstance(c, dict) and c.get("changed")
                    )
                    detail += (
                        f"; the upgrade from {resumed_from} already swapped"
                        f" {swapped or 'its services'} — the other run reconciles the units"
                    )
                report(f"stack sync: {detail}")
                return m.RunReport(
                    train=resolved_train, role=role, instance_id=instance_id,
                    started_at=started_at, finished_at=_now_iso(), auto=auto, dry_run=dry_run,
                    noop=False, services=(), health_ok=None, outcome="locked", detail=detail,
                )
        if auto:
            # Kill switch (#208): pause upgrades without touching launchd.
            marker = m.autoupgrade_disabled_path(home)
            if marker.exists():
                detail = f"auto-upgrade disabled by marker ({marker}) — nothing was changed"
                rpt = m.RunReport(
                    train=resolved_train, role=existing_cfg.role if existing_cfg else role,
                    instance_id=existing_cfg.instance_id if existing_cfg else instance_id,
                    started_at=started_at, finished_at=_now_iso(), auto=True, dry_run=dry_run,
                    noop=True, services=(), health_ok=None, outcome="noop", detail=detail,
                )
                write_report(home, rpt, dry_run=dry_run)
                report(f"stack sync: {detail}")
                return rpt
            # --auto never prompts: refuse BEFORE anything else (a gh error
            # would otherwise mask the real problem) and make any prompt
            # call a hard failure rather than a tty read.
            if existing_cfg is None:
                raise m.StackError(AUTO_NEEDS_STACK_JSON)
            prompt = _no_prompt
        preflight_gh_auth(runner)
        cfg = ensure_stack_config(home, existing=existing_cfg, auto=auto, dry_run=dry_run, prompt=prompt)
        instance_id = cfg.instance_id

        latest = latest_platform_tag(runner) if not train else ""
        resolved_train = m.resolve_train_version(train, latest)

        current_trains = {svc: read_current_train(home, svc) for svc in SERVICE_ORDER}

        if m.is_noop(resolved_train=resolved_train, current_trains=current_trains):
            # A no-op still health-checks (#210 review): the --auto timer's
            # exit code is the monitoring signal, and an up-to-date spoke with
            # a dead gateway must not report success indefinitely. And it
            # reconciles EVERY stack-managed unit (#303) — the same pass the
            # full path runs — so a unit a newer train introduced (or one an
            # operator removed) is one `stack sync` away, never "wait for the
            # next train". Normally one short health poll (nothing was
            # kickstarted, a healthy stack answers immediately); when the
            # reconcile kicked something (a newly installed front, the
            # platform restarted for the #296 key) it waits out the boot like
            # an upgrade does.
            handoff = take_handoff(home) if resumed_from is not None and not dry_run else None
            if handoff and handoff.get("train") != resolved_train:
                handoff = None  # stale (a resumed run that lost the lock race)
            resumed_note = None
            if resumed_from is not None and handoff is None and not dry_run:
                # No swap record to resume (hand-passed flag, or a stale file
                # was discarded): this is an ordinary no-op reconcile — never
                # claim an upgrade that this process cannot vouch for.
                resumed_note = (
                    f"`--resumed-from {resumed_from}` given but no swap record was "
                    "found — treated as an ordinary no-op reconcile"
                )
                resumed_from = None
            units = reconcile_units(home, cfg, runner, dry_run=dry_run, timer_run=timer_run)
            kicked_any = any(u.kicked is not None for u in units)
            noop_diag: dict | None = None
            noop_health, noop_front = None, None
            if not dry_run:
                noop_health, noop_front, nprobes, npolls = stack_health(
                    runner, health_url, sleep=health_sleep,
                    **({"budget_s": 30.0} if kicked_any else {"attempts": 3}),
                )
                if noop_health is False:
                    noop_diag = capture_health_diagnostic(
                        runner, home, nprobes, budget_s=30.0 if kicked_any else 3.0, polls=npolls)
            unhealthy = noop_health is False
            front_down = noop_health is True and noop_front is False
            key_kick_failed = _key_kick_failed(units)
            if resumed_from is not None:
                head = (
                    f"upgrade from {resumed_from} finished under the {resolved_train} "
                    "CLI (re-exec after the platform swap)"
                )
                if handoff and handoff.get("detail"):
                    head += "; " + str(handoff["detail"])
            else:
                head = "already on the resolved train"
            if unhealthy:
                detail = (
                    f"{head}, but the gateway health "
                    "check FAILED — services may be down; check launchctl "
                    "and the service logs"
                    + ("" if resumed_from is not None else " (nothing was changed by sync)")
                )
            elif front_down:
                detail = f"{head}; " + FRONT_DOWN_DETAIL
            elif resumed_from is not None:
                detail = head
            else:
                detail = f"{head} — nothing to do"
            for note in _unit_notes(units):
                detail += "; " + note
            if resumed_note:
                detail += "; " + resumed_note
            env_drift = [u.name for u in units if u.drift]
            if handoff:
                env_drift = [*handoff.get("env_drift", ()), *(d for d in env_drift if d not in handoff.get("env_drift", ()))]
            if unhealthy or front_down or key_kick_failed:
                outcome = "needs_attention"
            else:
                # A resumed run IS the tail of an upgrade the old CLI applied.
                outcome = "ok" if resumed_from is not None else "noop"
            front_kicked = _front_kicked(units)
            if front_kicked is None and handoff:
                front_kicked = handoff.get("front_kickstarted")
            rpt = m.RunReport(
                train=resolved_train, role=cfg.role, instance_id=cfg.instance_id,
                started_at=str(handoff.get("started_at") or started_at) if handoff else started_at,
                finished_at=_now_iso(), auto=auto, dry_run=dry_run,
                noop=resumed_from is None,
                services=_handoff_services(handoff) if handoff else (),
                health_ok=noop_health, outcome=outcome, detail=detail,
                env_drift=tuple(env_drift), front_kickstarted=front_kicked,
                front_ok=noop_front, units=tuple(units), resumed_from=resumed_from,
                health_diagnostic=noop_diag,
            )
            write_report(home, rpt, dry_run=dry_run)
            if unhealthy:
                tail = "but the stack is UNHEALTHY"
            elif front_down:
                tail = "but the failover front is DOWN"
            elif key_kick_failed:
                tail = "but the platform kickstart for the front-health key FAILED"
            elif resumed_from is not None:
                tail = f"upgrade from {resumed_from} finished under the new CLI"
            else:
                tail = "nothing to do"
            report(f"stack sync: already on {resolved_train}, {tail}")
            return rpt

        if auto:
            # e2e43cfd: do not hammer a train whose last attempt REVERTED every
            # hour — wait for a newer train; a manual `stack sync` never takes
            # this path and always retries. Only `reverted`: every upgrade-path
            # `needs_attention` leaves current == target, so the next --auto
            # takes the no-op path before reaching here; the only way one gets
            # here is after a manual `--train vPrev` rollback, where freezing
            # train X for a front/key problem X did not cause would contradict
            # runbook 6a.
            last = last_attempt_for_train(home, resolved_train)
            if last is not None and last.get("outcome") == "reverted":
                detail = (
                    f"train {resolved_train} last attempt {last['outcome']} at "
                    f"{last.get('finished_at', '?')}; waiting for a newer train "
                    "(manual `stack sync` forces a retry)"
                )
                # A skipped spoke is a not-up-to-date spoke, not an unwatched
                # one: still reconcile units and health-check what is running.
                units = reconcile_units(home, cfg, runner, dry_run=dry_run, timer_run=timer_run)
                skip_health, skip_diag = None, None
                if not dry_run:
                    kicked_any = any(u.kicked is not None for u in units)
                    skip_health, _skip_front, sprobes, spolls = stack_health(
                        runner, health_url, sleep=health_sleep,
                        **({"budget_s": 30.0} if kicked_any else {"attempts": 3}),
                    )
                    if skip_health is False:
                        skip_diag = capture_health_diagnostic(
                            runner, home, sprobes, budget_s=30.0 if kicked_any else 3.0, polls=spolls)
                skip_outcome = "needs_attention" if skip_health is False else "skipped"
                if skip_outcome == "needs_attention":
                    detail += (
                        "; the gateway health check FAILED on the running train — "
                        "services may be down; check launchctl and the service logs"
                    )
                for note in _unit_notes(units):
                    detail += "; " + note
                rpt = m.RunReport(
                    train=resolved_train, role=cfg.role, instance_id=cfg.instance_id,
                    started_at=started_at, finished_at=_now_iso(), auto=True, dry_run=dry_run,
                    noop=True, services=(), health_ok=skip_health, outcome=skip_outcome,
                    detail=detail, units=tuple(units), health_diagnostic=skip_diag,
                )
                write_report(home, rpt, dry_run=dry_run)
                report(f"stack sync: {detail}")
                return rpt

        plans = {
            svc: m.plan_service_swap(svc, current_train=current_trains[svc], target_train=resolved_train)
            for svc in SERVICE_ORDER
        }

        if dry_run:
            for svc in SERVICE_ORDER:
                p = plans[svc]
                report(
                    f"  {svc:<12} {p.previous_train or '(none)':<10} -> {p.target_train}"
                    f"{'  [build+swap]' if p.changes else '  [unchanged]'}"
                )
            front_env_exists = m.env_file_path(home, m.FRONT_SERVICE).exists()
            report(
                f"  {m.FRONT_SERVICE:<12} would render "
                f"{m.plist_path(home, m.FRONT_SERVICE).name}"
                + ("" if front_env_exists else f" + {m.FRONT_SERVICE}.env")
                + (
                    " and kickstart it (runs from the platform venv)"
                    if plans[m.FRONT_VENV_SERVICE].changes or not front_env_exists
                    else " (kickstart only if the plist changed)"
                )
            )
            units = reconcile_units(
                home, cfg, runner, dry_run=True, timer_run=timer_run,
                platform_swapped=plans[m.FRONT_VENV_SERVICE].changes,
            )
            timer = next(u for u in units if u.name == m.AUTOUPGRADE_UNIT)
            report(f"  {m.AUTOUPGRADE_UNIT:<12} " + (timer.note or "timer plist unchanged"))
            platform_plan = plans["platform"]
            if not resumed and m.should_reexec(
                outcome="ok", platform_swapped=platform_plan.changes,
                previous_platform_train=platform_plan.previous_train,
                target_train=resolved_train,
                running_under_target=running_under(
                    m.service_venv_dir(home, "platform", resolved_train)),
                resumed=False,
            ):
                report(
                    f"  would re-exec {new_cli_path(home)} (the {resolved_train} CLI) "
                    "after a healthy swap, to reconcile the new train's units"
                )
            rpt = m.RunReport(
                train=resolved_train, role=cfg.role, instance_id=cfg.instance_id,
                started_at=started_at, finished_at=_now_iso(), auto=auto, dry_run=True,
                noop=False,
                services=tuple(
                    m.ServiceChange(service=p.service, previous_train=p.previous_train,
                                     target_train=p.target_train, changed=p.changes)
                    for p in plans.values()
                ),
                health_ok=None, outcome="ok",
                detail="dry run: nothing downloaded, built, swapped, or kickstarted",
                units=tuple(units),
            )
            return rpt

        # -- real run --------------------------------------------------------
        assets_dir = m.app_support_dir(home) / "downloads" / resolved_train / "_manifest"
        manifest = fetch_train_manifest(runner, resolved_train, into=assets_dir)
        repo_tags = unique_repo_tags(manifest)
        for repo, tag in repo_tags:
            fetch_component_assets(runner, repo=repo, tag=tag, into=download_dir(home, resolved_train, repo, tag))
        find_links = [download_dir(home, resolved_train, repo, tag) for repo, tag in repo_tags]

        built: dict[str, Path] = {}
        for svc in SERVICE_ORDER:
            if not plans[svc].changes:
                continue
            report(f"build {svc}")
            built[svc] = build_service_venv(
                runner, home=home, train=resolved_train, manifest=manifest, service=svc,
                find_links=find_links, report=report,
            )

        if "platform" in built:
            platform_entry = manifest.service("platform")
            report("unpack dashboard dist")
            unpack_dashboard(
                runner,
                tarball=dashboard_tarball_path(home, resolved_train, platform_entry),
                dest=m.dashboard_dir(home, resolved_train),
            )
            swap_dashboard_current(home, resolved_train)

        # migration-head probe BEFORE any swap.
        old_heads: dict[str, list[str] | None] = {}
        new_heads: dict[str, list[str]] = {}
        for svc, venv_dir in built.items():
            prev = plans[svc].previous_train
            if prev is not None:
                old_python = m.service_venv_dir(home, svc, prev) / "bin" / "python"
                old_heads[svc] = probe_heads(runner, old_python, svc)
            else:
                old_heads[svc] = None
            new_heads[svc] = probe_heads(runner, venv_dir / "bin" / "python", svc)

        changes: list[m.ServiceChange] = []
        for svc in SERVICE_ORDER:
            plan = plans[svc]
            if not plan.changes:
                changes.append(
                    m.ServiceChange(service=svc, previous_train=plan.previous_train,
                                     target_train=plan.target_train, changed=False)
                )
                continue
            crossed = m.migration_crossed(old_heads[svc], new_heads[svc])
            swap_current(home, svc, built[svc])
            changes.append(
                m.ServiceChange(service=svc, previous_train=plan.previous_train,
                                 target_train=plan.target_train, changed=True,
                                 migration_crossed=crossed)
            )

        # Every stack-managed unit (#303) — rendered here, after the swap and
        # BEFORE the services' env drift pass (the platform's front-health key
        # must be appended first, so an old platform.env re-matches the new
        # template), kicked below AFTER the services they run from.
        units = reconcile_units(
            home, cfg, runner, dry_run=False, timer_run=timer_run,
            platform_swapped=plans[m.FRONT_VENV_SERVICE].changes, kick=False,
        )

        # env + plist refresh — every service, every non-noop run (spec §5
        # step 3/4). The dashboard env value is the STABLE `dashboard/current`
        # symlink, never a train-versioned path (#210 review: env files are
        # operator-owned and never clobbered, so a versioned value would pin
        # the UI to the install-time train forever).
        variables = env_variables(
            cfg, dashboard_dist=str(m.dashboard_root(home) / "current")
        )
        env_drift: list[str] = []
        for svc in SERVICE_ORDER:
            result = apply_env_file(home, svc, variables=variables, dry_run=False)
            if result.action == "drifted":
                env_drift.append(svc)
            apply_plist(home, svc, dry_run=False)
        env_drift += [u.name for u in units if u.drift]

        ensure_databases(runner, report)

        kicked: dict[str, bool] = {}
        for i, change in enumerate(changes):
            if change.changed:
                ok = kickstart_service(runner, home, change.service)
                kicked[change.service] = ok
                changes[i] = m.ServiceChange(
                    service=change.service, previous_train=change.previous_train,
                    target_train=change.target_train, changed=True,
                    migration_crossed=change.migration_crossed, kickstarted=ok,
                )
        # A unit's kick reuses a service kick this run already made (a key
        # newly written into a CHANGED platform is picked up by its upgrade
        # kick); an unchanged platform is restarted so the running process
        # (env is sourced at exec time) picks the key up.
        units = kick_units(runner, home, units, dry_run=False, already_kicked=kicked)
        # Attribute a failed platform kick honestly: when the platform was
        # kicked for its own UPGRADE and that failed, it is the upgrade kick
        # that failed (the service row carries kickstarted=False), not the key.
        platform_upgrade_kick_failed = kicked.get("platform") is False
        key_kick_failed = _key_kick_failed(units) and not platform_upgrade_kick_failed
        front_kicked = _front_kicked(units)

        budget = upgrade_health_budget_s()
        health_ok, front_ok, probes, polls = stack_health(
            runner, health_url, budget_s=budget, sleep=health_sleep
        )
        health_diagnostic: dict | None = None
        if not health_ok:
            health_diagnostic = capture_health_diagnostic(
                runner, home, probes, budget_s=budget, polls=polls)
            slow = health_diagnostic["not_answering"]
            report(
                f"gateway not healthy after {polls} polls ({budget:g} s budget); services not answering: "
                f"{', '.join(slow) or 'none (gateway only)'}"
            )
        any_crossed = any(c.migration_crossed for c in changes if c.changed)
        revertible = all(c.previous_train is not None for c in changes if c.changed)

        if health_ok:
            outcome = "ok"
        elif any_crossed or not revertible:
            outcome = "needs_attention"
        else:
            outcome = "reverted"

        if outcome == "reverted":
            report("post-upgrade health check failed, no migration crossed — auto-reverting")
            for c in changes:
                if c.changed and c.previous_train is not None:
                    swap_current(home, c.service, m.service_venv_dir(home, c.service, c.previous_train))
                    kickstart_service(runner, home, c.service)
                    # Park the failed train's venv (33f5b3da): `current` is back
                    # on the previous train, and the next attempt must start
                    # from a clean target dir. Kept for autopsy, never deleted.
                    aside = set_aside_reverted_venv(home, c.service, c.target_train)
                    if aside is not None:
                        report(f"kept reverted {c.service} venv at {aside}")
                    prune_reverted_venvs(home, c.service)
            # the front runs from the platform venv — follow it back.
            if plans[m.FRONT_VENV_SERVICE].changes:
                kickstart_service(runner, home, m.FRONT_SERVICE)
            # The previous train gets the same budget: a revert whose old
            # train needs 45 s to boot must not read as "the revert failed too".
            health_ok, front_ok, rprobes, rpolls = stack_health(
                runner, health_url, budget_s=budget, sleep=health_sleep
            )
            if not health_ok and health_diagnostic is not None:
                health_diagnostic["post_revert"] = capture_health_diagnostic(
                    runner, home, rprobes, budget_s=budget, polls=rpolls)

        if outcome == "ok":
            for svc in SERVICE_ORDER:
                plan = plans[svc]
                keep = {resolved_train}
                if plan.previous_train:
                    keep.add(plan.previous_train)
                gc_service_trains(home, svc, keep=keep)

        detail = None
        if outcome == "needs_attention":
            # Two distinct causes, two distinct recovery paths — never claim a
            # migration crossed when the real reason is "nothing to revert to"
            # (#210 review: the fabricated re-seed pointer sent a fresh-install
            # failure down entirely the wrong runbook).
            crossed_services = [c.service for c in changes if c.migration_crossed]
            if crossed_services:
                detail = (
                    "post-upgrade health check failed AND a migration crossed "
                    f"for: {', '.join(crossed_services)} — NOT auto-reverted "
                    "(boot-migrate is forward-only). State left as-is; this "
                    "needs a human (re-seed protocol, replication-continuity "
                    "§7)."
                )
            else:
                detail = (
                    "post-upgrade health check failed and there is no previous "
                    "train to revert to (first install of at least one "
                    "service). No migration crossed and nothing was reverted "
                    "or re-seeded — check launchctl and the service logs, "
                    "then re-run sync."
                )
        elif outcome == "reverted":
            detail = (
                "post-upgrade health check failed, no migration crossed — "
                "symlinks auto-reverted and kickstarted back"
            )

        if outcome == "ok" and front_ok is False:
            # The train is good (gateway healthy, kept, GC'd); only the front
            # is down — report it loudly, never revert for it.
            outcome = "needs_attention"
            detail = "upgrade applied; " + FRONT_DOWN_DETAIL
        if key_kick_failed and outcome == "ok":
            outcome = "needs_attention"
            detail = "upgrade applied"
        elif platform_upgrade_kick_failed and outcome == "ok":
            outcome = "needs_attention"
            detail = (
                "upgrade applied, but the platform's kickstart FAILED — the old "
                "process is still serving; run `launchctl kickstart -k "
                "gui/$(id -u)/dev.snowline.platform` and check its err.log"
            )

        for note in _unit_notes(units):
            detail = (detail + "; " if detail else "") + note

        platform_plan = plans["platform"]
        reexec_wanted = m.should_reexec(
            outcome=outcome, platform_swapped=platform_plan.changes,
            previous_platform_train=platform_plan.previous_train,
            target_train=resolved_train,
            running_under_target=running_under(
                m.service_venv_dir(home, "platform", resolved_train)),
            resumed=resumed,
        )
        cli = new_cli_path(home)
        if reexec_wanted and not os.access(cli, os.X_OK):
            reexec_wanted = False
            detail = (detail + "; " if detail else "") + (
                f"the new train's CLI was not found at {cli} — finished under "
                "this process's CLI instead (no re-exec); run `snowline stack "
                "sync` once more so the new train reconciles its own units"
            )
        rpt = m.RunReport(
            train=resolved_train, role=cfg.role, instance_id=cfg.instance_id,
            started_at=started_at, finished_at=_now_iso(), auto=auto, dry_run=False,
            noop=False, services=tuple(changes), health_ok=health_ok, outcome=outcome,
            detail=detail, env_drift=tuple(env_drift), front_kickstarted=front_kicked,
            front_ok=front_ok, units=tuple(units), health_diagnostic=health_diagnostic,
        )
        if reexec_wanted:
            # Hand the rest of the run to the NEW train's code (#303): it
            # takes the lock, sees the train already current (no-op path),
            # reconciles every unit with ITS unit list, re-checks health and
            # writes the run's single report. This process writes none.
            argv = reexec_argv(
                cli, role=role, train=resolved_train, auto=auto,
                health_url=health_url, verbose=verbose,
                resumed_from=platform_plan.previous_train or "none",
            )
            write_handoff(home, rpt)
            report(f"stack sync: platform swapped to {resolved_train}; re-exec'ing {cli} to finish the run")
            if lock is not None:
                lock.release()
                lock = None
            os.environ[m.SYNC_RESUMED_ENV] = "1"
            sys.stdout.flush()
            sys.stderr.flush()
            try:
                os.execv(str(cli), argv)
            except OSError as exc:
                os.environ.pop(m.SYNC_RESUMED_ENV, None)
                m.sync_handoff_path(home).unlink(missing_ok=True)
                rpt = replace(rpt, detail=(rpt.detail + "; " if rpt.detail else "") + (
                    f"re-exec of {cli} failed ({exc}) — finished under this "
                    "process's CLI; run `snowline stack sync` once more so the "
                    "new train reconciles its own units"
                ))
                write_report(home, rpt, dry_run=False)
                return rpt
            # Only reachable under a test double of os.execv.
            return rpt
        write_report(home, rpt, dry_run=False)
        return rpt

    except (m.StackError, CommandError) as exc:
        rpt = m.RunReport(
            train=resolved_train, role=role, instance_id=instance_id,
            started_at=started_at, finished_at=_now_iso(), auto=auto, dry_run=dry_run,
            noop=False, services=(), health_ok=None, outcome="failed", detail=str(exc),
        )
        write_report(home, rpt, dry_run=dry_run)
        return rpt
    finally:
        if lock is not None:
            lock.release()
