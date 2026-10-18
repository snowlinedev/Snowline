"""Pure logic behind `snowline stack sync` (macOS distribution spec §5/§6,
item b70b0359 / issue #203).

Mirrors `release/model.py`'s split: everything here is a function of its
arguments — no subprocess, no network, no clock, no `Path.home()` read here
(every path is a function of an explicit `home`). `sync.py` owns every side
effect (git/gh/uv/launchctl/createdb via the SAME `release.runner.Runner`
seam, plus filesystem writes) and calls into this module for every decision,
which is what lets the interesting rules — train resolution, migration-crossed
detection, the symlink-swap plan, the spoke-only guard, the auto-mode
failure-posture split, env-template drift — be tested without a real
Postgres, `gh` token, or launchd.

Vocabulary carried over from `release/model.py`: a SERVICE is a wheel/venv
unit (platform, governance, memory, pm). `kind="library"` (the sdk) is an
install *input* for the others, never a venv of its own (spec §5 step 2).
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from snowline_platform.replication import INGEST_PATH as _PLATFORM_INGEST_PATH
from snowline_platform.replication import via_base as _via_base

_VERSION_RE = re.compile(r"^v0\.(\d+)\.(\d+)$")


class StackError(Exception):
    """Anything sync refuses to do, phrased for the operator."""


class SpokePostureError(StackError):
    """Refused: this instance (or the requested role) is not a v1 spoke
    (macOS distribution spec §6; decision 54447516 — the hub stays
    source-run, the packaged channel is spoke-only)."""


# --------------------------------------------------------------------------
# Layout — every path is a function of `home`, never read from the real
# filesystem here.
# --------------------------------------------------------------------------

APP_SUPPORT_RELPATH = Path("Library/Application Support/Snowline")
CONFIG_RELPATH = Path(".config/snowline")
LAUNCH_AGENTS_RELPATH = Path("Library/LaunchAgents")
LOCAL_BIN_RELPATH = Path(".local/bin")

# Loopback ports for the v1 spoke service set (ops/roam precedent for
# platform/governance/memory; pm is NEW to the packaged spoke — roam predates
# pm as a spoke service (spec §8) — and takes the next port in that sequence).
SERVICE_PORTS: Mapping[str, int] = {
    "platform": 8848,
    "governance": 8801,
    "memory": 8802,
    "pm": 8803,
}

# The full trusted-CIDR set, stated IN FULL (ops/roam §5.1 config trap: this
# env REPLACES the platform's default when set, so a dropped entry here is a
# silent outage, not a graceful fallback).
TRUSTED_CIDRS = "100.64.0.0/10,127.0.0.0/8,::1"

# service -> top-level installed package, for the migration-head probe
# (`import <pkg>`, then `<pkg's dir>/migrations`) — the same package-internal
# resolution `release/_smoke_boot.py::_assert_at_head` uses.
SERVICE_PACKAGE: Mapping[str, str] = {
    "platform": "snowline_platform",
    "governance": "snowline_governance",
    "memory": "snowline_memory",
    "pm": "snowline_pm",
}

# service -> ASGI target, the same module:app convention ops/roam/run-service.sh
# uses for platform/governance/memory, extended to pm.
SERVICE_ASGI_APP: Mapping[str, str] = {
    "platform": "snowline_platform.app:app",
    "governance": "snowline_governance.app:app",
    "memory": "snowline_memory.app:app",
    "pm": "snowline_pm.app:app",
}

DB_NAME: Mapping[str, str] = {svc: f"snowline_{svc}" for svc in SERVICE_PACKAGE}

# The spoke's loopback failover front (issue #260, `snowline_platform.front`).
# Deliberately NOT in SERVICE_PORTS: SERVICE_ORDER (and with it the venv
# builds, the seed participants, the DBs, the env-posture guards) derives from
# that map, and the front is none of those things — no wheel of its own (it
# runs from the PLATFORM's venv), no database, no replication stream, never
# registers with the platform. It is a stack-managed launchd unit beside the
# services: its plist is rendered by the same renderer, `stack sync` installs
# and kickstarts it, and the post-boot health checks probe its own
# `/front/health`. It takes the hub gateway's port (8850) on loopback so the
# MBP's Claude Code MCP entry reads the same path as the hub's. Spoke-only by
# construction: `stack sync` refuses anything but role=spoke (decision
# 54447516), so the hub never renders or loads it.
FRONT_SERVICE = "front"
FRONT_PORT = 8850
FRONT_VENV_SERVICE = "platform"
FRONT_ASGI_FACTORY = "snowline_platform.front:create_app"
FRONT_HEALTH_PATH = "/front/health"


def front_health_url(port: int = FRONT_PORT) -> str:
    return f"http://127.0.0.1:{port}{FRONT_HEALTH_PATH}"

KEEP_TRAINS = 2


def app_support_dir(home: Path) -> Path:
    return home / APP_SUPPORT_RELPATH


def venvs_root(home: Path) -> Path:
    return app_support_dir(home) / "venvs"


def service_dir(home: Path, service: str) -> Path:
    return venvs_root(home) / service


def service_venv_dir(home: Path, service: str, train: str) -> Path:
    return service_dir(home, service) / train


def service_current_link(home: Path, service: str) -> Path:
    return service_dir(home, service) / "current"


def dashboard_root(home: Path) -> Path:
    return app_support_dir(home) / "dashboard"


def dashboard_dir(home: Path, train: str) -> Path:
    return dashboard_root(home) / train


def config_dir(home: Path) -> Path:
    return home / CONFIG_RELPATH


def stack_config_path(home: Path) -> Path:
    return config_dir(home) / "stack.json"


def env_file_path(home: Path, service: str) -> Path:
    return config_dir(home) / f"{service}.env"


def launch_agents_dir(home: Path) -> Path:
    return home / LAUNCH_AGENTS_RELPATH


SYSTEM_LAUNCH_DAEMONS_DIR = Path("/Library/LaunchDaemons")


def plist_label(service: str) -> str:
    return f"dev.snowline.{service}"


def plist_path(home: Path, service: str) -> Path:
    return launch_agents_dir(home) / f"{plist_label(service)}.plist"


def local_bin_dir(home: Path) -> Path:
    return home / LOCAL_BIN_RELPATH


def snowline_symlink(home: Path) -> Path:
    return local_bin_dir(home) / "snowline"


def log_dir(home: Path) -> Path:
    return app_support_dir(home) / "logs"


def service_err_log_path(home: Path, service: str) -> Path:
    """The launchd stderr log `render_plist` points a service at (its
    `StandardErrorPath`) — where a plugin that crashed after `/health`
    answered left its traceback."""
    return log_dir(home) / f"snowline-{service}.err.log"


def report_path(home: Path) -> Path:
    return app_support_dir(home) / "sync-report.json"


def report_history_dir(home: Path) -> Path:
    return app_support_dir(home) / "sync-history"


# --------------------------------------------------------------------------
# The train manifest (release/train.json, spec §4) — the target-side reader.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceEntry:
    service: str
    repo: str
    tag: str
    sha: str
    wheel: str
    kind: str = "service"

    @property
    def is_service(self) -> bool:
        return self.kind == "service"


@dataclass(frozen=True)
class TrainManifest:
    version: str
    services: tuple[ServiceEntry, ...]

    def service(self, name: str) -> ServiceEntry:
        for svc in self.services:
            if svc.service == name:
                return svc
        raise StackError(f"train {self.version} manifest has no entry for service {name!r}")

    def sdk(self) -> ServiceEntry:
        for svc in self.services:
            if svc.kind == "library":
                return svc
        raise StackError(f"train {self.version} manifest carries no library-kind (sdk) entry")

    def installable_services(self) -> tuple[ServiceEntry, ...]:
        """Services sync builds a venv for — `kind="library"` (the sdk) is an
        install input, not a venv of its own (spec §5 step 2)."""
        return tuple(s for s in self.services if s.is_service)

    def repos(self) -> tuple[str, ...]:
        seen: list[str] = []
        for svc in self.services:
            if svc.repo not in seen:
                seen.append(svc.repo)
        return tuple(seen)


def validate_train_version(version: str) -> str:
    if not _VERSION_RE.match(version):
        raise StackError(
            f"train {version!r} is not the v0.MINOR.PATCH scheme (macOS "
            "distribution spec §4)"
        )
    return version


def parse_train_manifest(data: Mapping[str, object]) -> TrainManifest:
    version = validate_train_version(str(data.get("version") or ""))
    raw = data.get("components")
    if not isinstance(raw, Mapping) or not raw:
        raise StackError(f"train {version} manifest has no `components` map")
    services = tuple(
        ServiceEntry(
            service=str(name),
            repo=str(entry["repo"]),
            tag=str(entry["tag"]),
            sha=str(entry["sha"]),
            wheel=str(entry["wheel"]),
            kind=str(entry.get("kind", "service")),
        )
        for name, entry in raw.items()
    )
    return TrainManifest(version=version, services=services)


def resolve_train_version(explicit: str | None, latest: str) -> str:
    """`--train` wins when given; otherwise the platform repo's latest
    release (spec §5 step 1). Rollback (`--train vPrev`) is this SAME call —
    an older explicit train is not distinguished here, sync's caller decides
    what "older" implies for migration-crossed handling."""
    return validate_train_version(explicit) if explicit else validate_train_version(latest)


# --------------------------------------------------------------------------
# stack.json — machine-specific config, prompted once, re-read every run
# (spec §6).
# --------------------------------------------------------------------------

STACK_CONFIG_SCHEMA_VERSION = 1
VALID_ROLES = ("spoke",)


@dataclass(frozen=True)
class StackConfig:
    role: str
    instance_id: str
    primary_tailnet_address: str
    # Added by `snowline stack bootstrap-spoke` (item 71317cd6) — ADDITIVE to
    # the schema_version-1 shape `sync` writes: a stack.json from before this
    # item has neither field, and that must stay a loadable file (a missing
    # key means "prompt for it", not "reject the file", spec §6/#211 review).
    # `primary_gateway_url`: the primary's FULL gateway URL (scheme+host+port)
    # — the hub binds :8850 in practice, which is NOT the roam runbook's
    # illustrative :8848, so this cannot be derived from
    # `primary_tailnet_address` alone without a port; bootstrap-spoke prompts
    # for it once with a default built from `primary_tailnet_address` (§ see
    # `default_primary_gateway_url`).
    # `local_tailnet_address`: THIS machine's own tailnet host/IP — needed so
    # the primary can dial the spoke's ingest endpoints during seeding (§7
    # step 1); nothing else in stack.json carries it, since `sync` never
    # needs to address this machine from outside.
    primary_gateway_url: str | None = None
    local_tailnet_address: str | None = None

    def to_json(self) -> str:
        data: dict[str, object] = {
            "schema_version": STACK_CONFIG_SCHEMA_VERSION,
            "role": self.role,
            "instance_id": self.instance_id,
            "primary_tailnet_address": self.primary_tailnet_address,
        }
        # Only written once bootstrap-spoke has them — an old-shape file that
        # never ran bootstrap-spoke stays byte-for-byte old-shape (no null
        # clutter), and `sync` (which never touches these fields) leaves them
        # untouched on any file that already carries them.
        if self.primary_gateway_url is not None:
            data["primary_gateway_url"] = self.primary_gateway_url
        if self.local_tailnet_address is not None:
            data["local_tailnet_address"] = self.local_tailnet_address
        return json.dumps(data, indent=2, sort_keys=False) + "\n"


def parse_stack_config(data: Mapping[str, object]) -> StackConfig:
    role = str(data.get("role") or "")
    instance_id = str(data.get("instance_id") or "")
    primary = str(data.get("primary_tailnet_address") or "")
    missing = [
        name
        for name, val in (
            ("role", role),
            ("instance_id", instance_id),
            ("primary_tailnet_address", primary),
        )
        if not val
    ]
    if missing:
        raise StackError(
            f"stack.json is missing required field(s): {', '.join(missing)}"
        )
    # Additive fields (bootstrap-spoke, item 71317cd6): absent on any
    # schema_version-1 file written before this item — that is not an error,
    # it is "not bootstrapped yet", so it loads as None rather than raising.
    gateway = data.get("primary_gateway_url")
    local_addr = data.get("local_tailnet_address")
    return StackConfig(
        role=role,
        instance_id=instance_id,
        primary_tailnet_address=primary,
        primary_gateway_url=str(gateway) if gateway else None,
        local_tailnet_address=str(local_addr) if local_addr else None,
    )


def load_stack_config(text: str | None) -> StackConfig | None:
    if text is None:
        return None
    return parse_stack_config(json.loads(text))


# --------------------------------------------------------------------------
# Spoke-only posture (decision 54447516) — refused before ANYTHING is
# touched, ahead of even the gh-auth preflight.
# --------------------------------------------------------------------------


def check_spoke_posture(*, requested_role: str, existing: StackConfig | None) -> None:
    if requested_role not in VALID_ROLES:
        raise SpokePostureError(
            f"--role {requested_role!r} is not supported — `snowline stack "
            "sync` is spoke-only in v1 (macOS distribution spec §6; the hub "
            "stays source-run per decision 54447516)"
        )
    if existing is not None and existing.role != "spoke":
        raise SpokePostureError(
            f"~/.config/snowline/stack.json declares role={existing.role!r} "
            "— refusing to run sync against an instance configured as "
            "primary (decision 54447516: the packaged channel is spoke-only)"
        )


def check_no_primary_env(existing_env_texts: Mapping[str, str]) -> None:
    """A second, filesystem-level guard: a hand-configured primary env file
    (ops/roam/env.primary.example's pattern, `SNOWLINE_INSTANCE_ID=primary`)
    must refuse sync even before a stack.json exists — this is exactly the
    posture the live hub carries today."""
    for service, text in existing_env_texts.items():
        instance_id = parse_env_exports(text).get("SNOWLINE_INSTANCE_ID")
        if instance_id == "primary":
            raise SpokePostureError(
                f"{service}.env already declares SNOWLINE_INSTANCE_ID=primary "
                "— refusing to run spoke sync against a hand-configured "
                "primary instance (decision 54447516)"
            )


# --------------------------------------------------------------------------
# Migration-crossed detection (auto-upgrade failure posture; spec §5
# rollback split).
# --------------------------------------------------------------------------


def migration_crossed(old_heads: Sequence[str] | None, new_heads: Sequence[str]) -> bool:
    """Whether a schema migration crossed for one service between the old
    and new venv. No prior venv (`old_heads is None` — e.g. a fresh install)
    is never "crossed": there is no prior state for the new one to diverge
    from."""
    if old_heads is None:
        return False
    return set(old_heads) != set(new_heads)


def any_migration_crossed(
    per_service_heads: Mapping[str, tuple[Sequence[str] | None, Sequence[str]]],
) -> bool:
    return any(migration_crossed(old, new) for old, new in per_service_heads.values())


# The probe runs inside EACH venv's OWN interpreter (old and new) — never
# imported into sync's own process, which may not have that venv's package
# installed at all, or may be running a different version of it. Mirrors
# `_smoke_boot.py::_assert_at_head`'s package-internal migrations-dir
# resolution, applied to a bare `import` instead of a booted app (sync only
# needs the package's alembic head set, not a live DB connection).
_HEAD_PROBE_SRC = (
    "import importlib, json, pathlib\n"
    "from alembic.script import ScriptDirectory\n"
    "m = importlib.import_module({package!r})\n"
    "root = pathlib.Path(m.__file__).resolve().parent\n"
    "migrations = root / 'migrations'\n"
    "if not migrations.exists():\n"
    "    migrations = root.parent / 'migrations'\n"
    "print(json.dumps(sorted(ScriptDirectory(str(migrations)).get_heads())))\n"
)


def head_probe_argv(python: Path, service: str) -> list[str]:
    package = SERVICE_PACKAGE.get(service)
    if package is None:
        raise StackError(f"no package mapping for service {service!r}")
    return [str(python), "-c", _HEAD_PROBE_SRC.format(package=package)]


def parse_heads(probe_output: str) -> list[str]:
    return list(json.loads(probe_output.strip() or "[]"))


# --------------------------------------------------------------------------
# Symlink-swap planning + GC (spec §5 step 2 — keep 2 trains).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceSwapPlan:
    service: str
    previous_train: str | None  # None: no `current` link yet (fresh install)
    target_train: str

    @property
    def changes(self) -> bool:
        return self.previous_train != self.target_train


def plan_service_swap(service: str, *, current_train: str | None, target_train: str) -> ServiceSwapPlan:
    return ServiceSwapPlan(service=service, previous_train=current_train, target_train=target_train)


def trains_to_gc(installed_trains: Sequence[str], *, keep: Sequence[str]) -> list[str]:
    """Which per-service train dirs to remove — every installed train except
    the ones in `keep` (spec §5 step 2: keep 2 — the current train and the
    one it replaced)."""
    keep_set = set(keep)
    return [t for t in installed_trains if t not in keep_set]


# --------------------------------------------------------------------------
# Env templates (roam posture promoted from example to template, spec §5
# step 3 / risk 5) — never clobber an operator-edited file.
# --------------------------------------------------------------------------

_EXPORT_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env_exports(text: str) -> dict[str, str]:
    """`export KEY=VALUE` (or bare `KEY=VALUE`) lines -> a dict — a READ-ONLY
    approximation used by the posture guards and the drift report; plists no
    longer consume this (they SOURCE the env file at exec time, #210 review),
    so shell-fidelity gaps here can't corrupt a running service. Unquoted
    values have trailing ` # comment` text stripped (the guard-bypass bug:
    `export SNOWLINE_INSTANCE_ID=primary # hub` must parse as 'primary');
    `$VAR` expansion is deliberately NOT performed — callers compare literal
    tokens, never resolved paths."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _EXPORT_LINE_RE.match(stripped)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value[:1] in "\"'":
            # Quoted value: take the quoted content (anything after the
            # closing quote — including a trailing comment — is dropped).
            end = value.find(value[0], 1)
            if end != -1:
                value = value[1:end]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def render_env(template_text: str, variables: Mapping[str, str]) -> str:
    return string.Template(template_text).substitute(variables)


@dataclass(frozen=True)
class EnvFileResult:
    service: str
    path: Path
    # "written": no existing file, the rendered template was written.
    # "unchanged": existing file already matches what the template renders.
    # "drifted": existing (operator-edited or stale) file differs from the
    #   current template render — left alone, surfaced in the report.
    action: str
    rendered: str


def plan_env_file(*, service: str, path: Path, existing: str | None, rendered: str) -> EnvFileResult:
    if existing is None:
        return EnvFileResult(service=service, path=path, action="written", rendered=rendered)
    if existing == rendered:
        return EnvFileResult(service=service, path=path, action="unchanged", rendered=rendered)
    return EnvFileResult(service=service, path=path, action="drifted", rendered=rendered)


# --------------------------------------------------------------------------
# launchd plists — rendered fresh every run (unlike env files, plists are
# entirely sync-owned; spec §5 step 4).
# --------------------------------------------------------------------------


def render_plist(
    *,
    service: str,
    venv_current: Path,
    port: int,
    env_file: Path,
    logs: Path,
) -> str:
    """The env file is SOURCED AT EXEC TIME (`/bin/sh -c 'set -a; . env;
    exec uvicorn …'` — the live hub's own plist posture), never baked into
    `EnvironmentVariables` (#210 review): baking froze operator env edits
    until the next train, copied any credentials into a second
    world-readable location (`launchctl print` included), and forced a
    shell-semantics reimplementation in `parse_env_exports`. Sourcing keeps
    the env file the single, live source of truth."""
    app_target = SERVICE_ASGI_APP.get(service)
    if app_target is None:
        raise StackError(f"no ASGI target for service {service!r}")
    return _render_uvicorn_plist(
        unit=service,
        uvicorn_bin=venv_current / "bin" / "uvicorn",
        app_args=_sh_quote(app_target),
        port=port,
        env_file=env_file,
        logs=logs,
    )


def render_front_plist(
    *,
    platform_venv_current: Path,
    port: int = FRONT_PORT,
    env_file: Path,
    logs: Path,
) -> str:
    """The failover front's plist (issue #260) — the SAME renderer and posture
    as every service (env file sourced at exec time, loopback `--host`,
    KeepAlive), exec'ing the PLATFORM venv's uvicorn with the front's app
    factory. It follows the platform's `current` symlink, so a platform train
    swap is a front upgrade too (sync kickstarts it alongside)."""
    return _render_uvicorn_plist(
        unit=FRONT_SERVICE,
        uvicorn_bin=platform_venv_current / "bin" / "uvicorn",
        app_args=f"--factory {_sh_quote(FRONT_ASGI_FACTORY)}",
        port=port,
        env_file=env_file,
        logs=logs,
    )


def _render_uvicorn_plist(
    *, unit: str, uvicorn_bin: Path, app_args: str, port: int, env_file: Path, logs: Path
) -> str:
    label = plist_label(unit)
    out_log = logs / f"snowline-{unit}.out.log"
    err_log = logs / f"snowline-{unit}.err.log"
    launch = (
        f"set -a; . {_sh_quote(str(env_file))}; set +a; "
        f"exec {_sh_quote(str(uvicorn_bin))} {app_args} "
        f"--host 127.0.0.1 --port {port}"
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  launchd agent for the packaged spoke's {unit} (macOS distribution spec
  §5 step 4). Rendered by `snowline stack sync` from a packaged template —
  regenerated every run, never hand-edited (contrast the *.env files, which
  sync never clobbers and which this plist SOURCES at exec time, so operator
  env edits take effect on the next service restart). Execs
  {{venv}}/current/bin/uvicorn: no WorkingDirectory, nothing runs from a
  checkout (spec §2/§5).
-->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>{_xml_escape(launch)}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{_xml_escape(str(out_log))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(str(err_log))}</string>
</dict>
</plist>
"""


# The spoke's auto-upgrade timer (item 4a321bea / #208): a user LaunchAgent that
# runs `snowline stack sync --auto` hourly. Rendered/installed by sync itself.
AUTOUPGRADE_UNIT = "autoupgrade"
AUTOUPGRADE_INTERVAL_S = 3600
AUTOUPGRADE_PATH_EXTRAS = ("/opt/homebrew/opt/postgresql@16/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def autoupgrade_disabled_path(home: Path) -> Path:
    """Kill switch: while this file exists `stack sync --auto` is a no-op."""
    return config_dir(home) / "autoupgrade.disabled"


def autoupgrade_reload_pending_path(home: Path) -> Path:
    """Set when the timer plist changed during a timer-run sync (which must
    not reload itself); the next non-timer sync reloads and clears it."""
    return config_dir(home) / "autoupgrade.reload-pending"


def sync_lock_path(home: Path) -> Path:
    return app_support_dir(home) / "sync.lock"


def sync_handoff_path(home: Path) -> Path:
    """The old train's partial run report, handed to the NEW train's CLI
    when sync re-execs it after a platform swap (#303); consumed (and
    removed) by the resumed run, which writes the run's single report."""
    return app_support_dir(home) / "sync-handoff.json"


def autoupgrade_log_path(home: Path) -> Path:
    return log_dir(home) / "snowline-autoupgrade.log"


def render_autoupgrade_plist(*, home: Path, platform_venv_current: Path, logs: Path) -> str:
    """The timer plist. Execs the platform venv's `snowline` through the
    stable `current` symlink (like the front), so a train swap never strands
    the timer on an old venv. RunAtLoad is false and sync never kickstarts
    it: loading the agent must not start a sync inside a sync. Never
    prompts: `--auto` has no prompt path and launchd gives no tty."""
    label = plist_label(AUTOUPGRADE_UNIT)
    snowline_bin = platform_venv_current / "bin" / "snowline"
    # ~/.local/bin: where release/install.sh puts uv when it is not on PATH.
    path_value = ":".join(
        [str(platform_venv_current / "bin"), str(home / ".local" / "bin"), *AUTOUPGRADE_PATH_EXTRAS]
    )
    log = logs / "snowline-autoupgrade.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  Hourly auto-upgrade timer for the packaged spoke (item 4a321bea / #208).
  Rendered and (re)loaded by `snowline stack sync` / `bootstrap-spoke`,
  never hand-edited. Pause without touching launchd:
  touch ~/.config/snowline/autoupgrade.disabled
-->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_xml_escape(str(snowline_bin))}</string>
        <string>stack</string>
        <string>sync</string>
        <string>--auto</string>
    </array>
    <key>StartInterval</key>
    <integer>{AUTOUPGRADE_INTERVAL_S}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>{_xml_escape(str(home))}</string>
        <key>PATH</key>
        <string>{_xml_escape(path_value)}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{_xml_escape(str(log))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(str(log))}</string>
</dict>
</plist>
"""


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _sh_quote(text: str) -> str:
    """Single-quote for the plist's `/bin/sh -c` line."""
    return "'" + text.replace("'", "'\\''") + "'"


def gui_target(label: str, uid: int) -> str:
    """launchd's `gui/<uid>/<label>` bootstrap/kickstart target."""
    return f"gui/{uid}/{label}"


# --------------------------------------------------------------------------
# Run report (auto-upgrade requirements, work item body): machine-readable,
# written to a stable path + a timestamped history (spec hand-off, consumed
# by the failure reporting and the future launchd timer).
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_FAILED = 1


@dataclass(frozen=True)
class ServiceChange:
    service: str
    previous_train: str | None
    target_train: str
    changed: bool
    migration_crossed: bool = False
    kickstarted: bool | None = None  # None: not attempted (unchanged service)


@dataclass(frozen=True)
class UnitAction:
    """One stack-managed unit's outcome in a reconcile pass (#303): the
    front, the platform's front-health key, the auto-upgrade timer, … —
    every unit sync owns that is NOT a train-versioned service venv.

    `action` is the unit's own verb ("installed" | "updated" | "unchanged" |
    "added" | "deferred" | "healed" | …). `kick` names the launchd service
    whose kickstart this change requires (None: none); `kicked` is that
    kickstart's result (None: not attempted). `drift` = an operator-owned
    env file differs from the template; `note` is the human detail line."""

    name: str
    action: str
    kick: str | None = None
    kicked: bool | None = None
    drift: bool = False
    note: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "action": self.action, "kicked": self.kicked}


@dataclass(frozen=True)
class RunReport:
    train: str
    role: str
    instance_id: str
    started_at: str
    finished_at: str
    auto: bool
    dry_run: bool
    noop: bool
    services: tuple[ServiceChange, ...]
    health_ok: bool | None
    # "ok" | "noop" | "locked" | "skipped" | "reverted" | "needs_attention"
    # | "failed". `skipped` (e2e43cfd): an `--auto` fire that declined to
    # re-attempt a train whose last attempt reverted.
    outcome: str
    detail: str | None = None
    env_drift: tuple[str, ...] = field(default_factory=tuple)
    # The failover front (issue #260): None = not attempted (no-op/dry-run,
    # or nothing it depends on changed); True/False = kickstart result.
    front_kickstarted: bool | None = None
    # The front's `/front/health` — REPORTED, never a revert trigger (only
    # the gateway decides a train's fate). None = not checked (dry-run, or
    # the gateway itself failed first).
    front_ok: bool | None = None
    # Every stack-managed unit the run reconciled (#303), in order.
    units: tuple[UnitAction, ...] = field(default_factory=tuple)
    # Set when this run is the NEW train's CLI finishing an upgrade the OLD
    # train's CLI started (#303): the train that process was running.
    resumed_from: str | None = None
    # Captured before an unhealthy upgrade is reverted (4f9dfc47): per
    # service the err.log tail, `launchctl print` state and which health URLs
    # answered. None on every healthy / non-upgrade run.
    health_diagnostic: dict | None = None

    def to_dict(self) -> dict:
        d = self._base_dict()
        if self.health_diagnostic is not None:
            d["health_diagnostic"] = self.health_diagnostic
        return d

    def _base_dict(self) -> dict:
        return {
            "train": self.train,
            "role": self.role,
            "instance_id": self.instance_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "auto": self.auto,
            "dry_run": self.dry_run,
            "noop": self.noop,
            "outcome": self.outcome,
            "detail": self.detail,
            "health_ok": self.health_ok,
            "env_drift": list(self.env_drift),
            "front_kickstarted": self.front_kickstarted,
            "front_ok": self.front_ok,
            "resumed_from": self.resumed_from,
            "units": [u.to_dict() for u in self.units],
            "services": [
                {
                    "service": s.service,
                    "previous_train": s.previous_train,
                    "target_train": s.target_train,
                    "changed": s.changed,
                    "migration_crossed": s.migration_crossed,
                    "kickstarted": s.kickstarted,
                }
                for s in self.services
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n"


def exit_code(outcome: str) -> int:
    """0 = upgraded or already current; nonzero = failed, reason on stderr
    (work item body, --auto contract). A `reverted` run safely undid itself
    but did NOT reach the target train, so it counts as a failure for the
    caller (a launchd timer) even though state is clean."""
    return EXIT_OK if outcome in ("ok", "noop", "locked", "skipped") else EXIT_FAILED


# --------------------------------------------------------------------------
# Auto-mode semantics.
# --------------------------------------------------------------------------


def train_key(version: str) -> tuple[int, int]:
    """`v0.MINOR.PATCH` -> (MINOR, PATCH), for ordering trains."""
    match = _VERSION_RE.match(version)
    if not match:
        raise StackError(f"train {version!r} is not the v0.MINOR.PATCH scheme")
    return int(match.group(1)), int(match.group(2))


SYNC_RESUMED_ENV = "SNOWLINE_SYNC_RESUMED"


def should_reexec(
    *,
    outcome: str,
    platform_swapped: bool,
    previous_platform_train: str | None,
    target_train: str,
    running_under_target: bool,
    resumed: bool,
) -> bool:
    """Whether a full-path run hands the rest of the run to the NEW train's
    CLI (#303). The running process's code only knows the units ITS train
    manages, so an upgrade run by the old CLI can never install a unit the
    new train introduces; re-exec'ing the new CLI (which then takes the
    no-op path and reconciles every unit with the new unit list) fixes that
    by construction.

    Only after an `ok` run (a revert, or anything needing attention, is
    finished by the code that started it); only when the platform venv was
    swapped FORWARD (a rollback's target CLI is older code, which may not
    know `--resumed-from` and has no newer units to add); never when this
    process already runs from the target venv; and at most once."""
    if outcome != "ok" or not platform_swapped or resumed or running_under_target:
        return False
    if previous_platform_train is None:
        return True  # a first install: the target IS the newest code there is
    return train_key(target_train) > train_key(previous_platform_train)


def is_noop(*, resolved_train: str, current_trains: Mapping[str, str | None]) -> bool:
    """True when every installable service's `current` already points at the
    resolved train (work item body: "--auto must be a no-op when the train
    hasn't changed"). An empty `current_trains` (nothing installed yet) is
    never a no-op."""
    if not current_trains:
        return False
    return all(train == resolved_train for train in current_trains.values())


def decide_post_upgrade_outcome(*, health_ok: bool, migration_crossed: bool) -> str:
    """The auto-upgrade failure-posture split (work item body, Sean
    2026-08-30): health-check failure + NO migration crossed in the delta ->
    safe to auto-revert (`reverted`; the caller swaps symlinks back +
    kickstarts). Health-check failure + a migration crossed -> NEVER
    auto-revert (boot-migrate is forward-only; reverting code against an
    already-migrated DB is broken) — leave state exactly as it is and report
    loudly (`needs_attention`)."""
    if health_ok:
        return "ok"
    if migration_crossed:
        return "needs_attention"
    return "reverted"


# ==========================================================================
# `snowline stack bootstrap-spoke` (macOS distribution spec §6, item
# 71317cd6 / snowline-pm#122) — WRAPS `snowline replicate pair`/`seed`/
# `reseed-check` verbatim (docs/ops/roam-runbook.md §4-6): this module never
# reimplements any replication logic, only the argv/config that drives it and
# the local pre/postconditions around it. Deliberately separate from `sync`
# (spec §6: "software install and data topology are not fused") — the
# orchestration lives in `bootstrap.py`, mirroring the model/sync split above.
# ==========================================================================

# The hub's REAL gateway port (this decision's finding, corroborated by
# docs/specs/deploy-continuity.md §4 and replication-continuity.md's
# `localhost:8850` reference) — NOT the roam runbook's illustrative :8848
# (that is the SPOKE's own platform port, SERVICE_PORTS["platform"]). A
# `primary_gateway_url` therefore cannot be derived from
# `primary_tailnet_address` + SERVICE_PORTS; it needs its own default.
DEFAULT_PRIMARY_GATEWAY_PORT = 8850

# The primary's SNOWLINE_INSTANCE_ID, by convention across every doc in this
# repo (ops/roam/env.primary.example, the runbook's `--peer-instance primary`
# example) — v1 has exactly one primary, so this is not prompted.
PRIMARY_INSTANCE_ID = "primary"

# THE canonical spoke service tuple (#212 review: previously copied by hand
# in three places). `SERVICE_PORTS`' insertion order is the boot order
# (runbook §2), and everything else — sync's SERVICE_ORDER, the seed
# participants — derives from it, so a fifth replicating service added to
# SERVICE_PORTS is automatically installed, seeded, and kickstarted (or
# fails loudly), never silently skipped.
SERVICE_ORDER: tuple[str, ...] = tuple(SERVICE_PORTS)

# Participants seeded per the macOS distribution spec §7 table — platform
# (the scope stream) plus every replicating plugin, pm included (pm is NEW to
# the packaged spoke, spec §8).
SEED_PARTICIPANTS: tuple[str, ...] = SERVICE_ORDER

# Every SDK-based plugin serves the SDK's default ingest path (verified
# against pm's registration.py: REPLICATION_INGEST_PATH = "/events/ingest");
# the platform's own scope stream imports its constant rather than
# restating it (#212 review — comment-synced copies drift).
SDK_INGEST_PATH = "/events/ingest"
PARTICIPANT_INGEST_PATH: Mapping[str, str] = {
    "platform": _PLATFORM_INGEST_PATH,
    "governance": SDK_INGEST_PATH,
    "memory": SDK_INGEST_PATH,
    "pm": SDK_INGEST_PATH,
}

def default_primary_gateway_url(primary_tailnet_address: str) -> str:
    return f"http://{primary_tailnet_address}:{DEFAULT_PRIMARY_GATEWAY_PORT}"


def front_hub_url(cfg: StackConfig) -> str:
    """The hub gateway the failover front forwards to while it is healthy:
    stack.json's `primary_gateway_url` when bootstrap-spoke recorded one,
    else the same default bootstrap-spoke would have prompted with."""
    return cfg.primary_gateway_url or default_primary_gateway_url(cfg.primary_tailnet_address)


def check_bootstrap_stack_config(cfg: StackConfig | None) -> StackConfig:
    """Precondition 1, checked before anything else touches the filesystem or
    the network."""
    if cfg is None:
        raise StackError(
            "no ~/.config/snowline/stack.json — run `snowline stack sync` "
            "first (macOS distribution spec §5)"
        )
    return cfg


def check_local_services_installed(current_trains: Mapping[str, str | None]) -> None:
    """Precondition 2: every service's `current` symlink exists — i.e. at
    least one `snowline stack sync` has actually completed here. Order
    matches SEED_PARTICIPANTS."""
    missing = [svc for svc in SEED_PARTICIPANTS if current_trains.get(svc) is None]
    if missing:
        raise StackError(
            f"local service(s) not installed yet: {', '.join(missing)} — run "
            "`snowline stack sync` first (macOS distribution spec §5)"
        )


def check_local_gateway_healthy(health_ok: bool) -> None:
    """Precondition 3."""
    if not health_ok:
        raise StackError(
            "the local gateway is not healthy — bootstrap-spoke refuses to "
            "seed a spoke whose own services are not up; check launchctl and "
            "the service logs, then re-run"
        )


def check_primary_gateway_healthy(health_ok: bool, primary_gateway_url: str) -> None:
    """Precondition 4 — the primary is reachable and healthy over the
    tailnet before ANY pairing/seeding step runs (docs/ops/roam-runbook.md
    §5: seeding needs 'the primary up')."""
    if not health_ok:
        raise StackError(
            f"the primary's gateway at {primary_gateway_url} is not reachable "
            "or not healthy over the tailnet — check tailscaled on both "
            "machines and that the primary is up before bootstrapping a spoke "
            "(this is the 'primary unreachable at install time' failure mode, "
            "docs/ops/roam-runbook.md)"
        )


def check_pm_role_is_spoke(pm_env_text: str | None) -> None:
    """The pm-side verification the work item body asks for explicitly: read
    the RENDERED pm.env (never assume the template — an operator edit or a
    stale render both matter) and refuse loudly if it does not declare
    `SNOWLINE_PM_ROLE=spoke`."""
    if pm_env_text is None:
        raise StackError(
            "~/.config/snowline/pm.env does not exist — run `snowline stack "
            "sync` first so pm's env is rendered"
        )
    role = parse_env_exports(pm_env_text).get("SNOWLINE_PM_ROLE")
    if role != "spoke":
        raise StackError(
            f"pm.env declares SNOWLINE_PM_ROLE={role!r} (expected 'spoke') — "
            "refusing: bootstrap-spoke stands this instance up as a "
            "replication SPOKE of the primary, and a pm not configured as a "
            "spoke would seed against the wrong topology. Fix pm.env (or "
            "re-run `snowline stack sync`, which never overwrites an "
            "operator-edited env file — see the reported drift) before "
            "re-running bootstrap-spoke."
        )


def build_seed_config(cfg: StackConfig, *, local_platform_port: int) -> dict:
    """The `snowline replicate seed --config <this>.json` input
    (docs/ops/roam-runbook.md §5, `ops/roam/seed-config.example.json`'s
    shape) — built entirely from stack.json, with NO credential prompt.

    There is no primary-side Postgres user or dump URL to ask for any more:
    the primary serves its own snapshot over its replication-admin surface,
    authorized by the stream secret the seed mints while priming (item
    0ebe6a70; governance decision 1a83031c keeps the hub's Postgres
    loopback-only). The only Postgres URL left is the SPOKE's, which is local
    (peer auth, no credential)."""
    if not cfg.primary_gateway_url or not cfg.local_tailnet_address:
        raise StackError(
            "stack.json is missing primary_gateway_url/local_tailnet_address "
            "— bootstrap-spoke must resolve these before building a seed "
            "config (internal error: ensure_bootstrap_config was skipped)"
        )
    participants = {}
    for svc in SEED_PARTICIPANTS:
        path = PARTICIPANT_INGEST_PATH[svc]
        db = DB_NAME[svc]
        # Where the PRIMARY delivers to this participant on the spoke: the
        # spoke's ONE tailnet-reachable port — its platform's — with plugin
        # participants reached through the gateway's `/via/<plugin>` proxy
        # (decision 0b8390f7). The platform's own scope stream lives at the
        # gateway root. No plugin port is ever dialled across the tailnet.
        gateway = f"http://{cfg.local_tailnet_address}:{local_platform_port}"
        base = gateway if svc == "platform" else _via_base(gateway, svc)
        participants[svc] = {
            "spoke_ingest_url": f"{base}{path}",
            "spoke_db_url": f"postgresql:///{db}",
        }
    return {
        "primary": {"platform_url": cfg.primary_gateway_url, "instance": PRIMARY_INSTANCE_ID},
        "spoke": {
            "platform_url": f"http://127.0.0.1:{local_platform_port}",
            "instance": cfg.instance_id,
        },
        "participants": participants,
    }


def seed_config_path(home: Path) -> Path:
    return config_dir(home) / "seed.json"


def check_participants_registered(
    home: Path, registered: set[str] | None, *, spoke_platform_url: str, waited_s: float
) -> None:
    """The registration gate before §7 step 4 (#228): every seed participant
    except the platform (whose scope stream is self-declared, not registered)
    must appear in the spoke platform's `/plugins` with a `replication`
    block. `/health` answering is NOT registration — a plugin populates
    `/plugins` only from its heartbeat's first beat, and one that boots,
    answers health, then crashes never registers at all. `registered=None`
    means `/plugins` itself never answered."""
    expected = [svc for svc in SEED_PARTICIPANTS if svc != "platform"]
    if registered is None:
        raise StackError(
            f"the spoke platform's {spoke_platform_url}/plugins never answered "
            f"within {waited_s:.0f}s — refusing to reverse-pair; check "
            f"{service_err_log_path(home, 'platform')}"
        )
    missing = [svc for svc in expected if svc not in registered]
    if missing:
        detail = "; ".join(
            f"{svc} (see {service_err_log_path(home, svc)})" for svc in missing
        )
        raise StackError(
            f"participant(s) not registered with replication on the spoke "
            f"platform ({spoke_platform_url}/plugins) after {waited_s:.0f}s: "
            f"{detail} — the service answered /health but never registered (or "
            "crashed after). Fix it, kickstart it, then re-run bootstrap-spoke "
            "or `snowline replicate seed --config <seed.json> --reverse-pair "
            "--participants NAME` (idempotent)"
        )


# -- CLI argv builders (pure — the fake-runner tests assert these exactly) --


def seed_cli_argv(config_path: Path, *, reverse_pair: bool = False, reseed: bool = False) -> list[str]:
    argv = ["snowline", "replicate", "seed", "--config", str(config_path)]
    if reverse_pair:
        argv.append("--reverse-pair")
    if reseed:
        argv.append("--reseed")
    return argv


def reseed_check_cli_argv(config_path: Path) -> list[str]:
    return ["snowline", "replicate", "reseed-check", "--config", str(config_path)]


# -- machine-readable outcome (work item body: "a machine-readable outcome
# appended to the stack run-report history") — a PARALLEL report to
# `RunReport`, not a reuse of it: a bootstrap run is an ordered sequence of
# named steps (precondition checks, seed, boot, verify), not a per-service
# train swap, so `RunReport`'s shape (train/services/migration_crossed) does
# not fit without either stuffing steps into `detail` strings (losing
# machine-readability) or growing `RunReport` with bootstrap-only optional
# fields that would sit unused on every `sync` run. See the PR body for this
# call written out.


@dataclass(frozen=True)
class BootstrapStep:
    name: str
    # "ok" | "skipped" | "failed" | "warned" — `warned` is a probe that did
    # not pass but does not fail the run (the failover front's health, which
    # is reported, never a gate), so a consumer counting failed steps does not
    # see a failure on a successful bootstrap.
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class BootstrapReport:
    instance_id: str
    primary_gateway_url: str | None
    started_at: str
    finished_at: str
    dry_run: bool
    reseed: bool
    steps: tuple[BootstrapStep, ...]
    pm_role_ok: bool | None
    health_ok: bool | None
    # "ok" | "failed"
    outcome: str
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "primary_gateway_url": self.primary_gateway_url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "reseed": self.reseed,
            "outcome": self.outcome,
            "detail": self.detail,
            "pm_role_ok": self.pm_role_ok,
            "health_ok": self.health_ok,
            "steps": [
                {"name": s.name, "status": s.status, "detail": s.detail} for s in self.steps
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n"


def bootstrap_exit_code(outcome: str) -> int:
    return 0 if outcome == "ok" else 1


def bootstrap_report_path(home: Path) -> Path:
    return app_support_dir(home) / "bootstrap-report.json"


def bootstrap_report_history_dir(home: Path) -> Path:
    return app_support_dir(home) / "bootstrap-history"
