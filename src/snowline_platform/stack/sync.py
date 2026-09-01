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

import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from ..release import model as relm
from ..release import runner as relr
from ..release.runner import CommandError, Runner
from . import model as m

Report = Callable[[str], None]
PromptFn = Callable[[str], str]

PLATFORM_REPO = "snowlinedev/Snowline"
SERVICE_ORDER = ("platform", "governance", "memory", "pm")
DEFAULT_HEALTH_URL = "http://127.0.0.1:8848/health"

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


def check_not_source_hub(home: Path) -> None:
    """Refuse to install the packaged stack on a machine already running
    Snowline services sync does not manage — i.e. the source-run hub, whose
    posture lives in launchd plists, not in the `<service>.env` filenames the
    env guard reads (#210 review: without this, `curl … | sh` on the mini
    would bootstrap a SECOND service set onto the hub's own ports). A
    stack-managed spoke always has stack.json, so its own plists don't trip
    this."""
    if m.stack_config_path(home).exists():
        return
    agents = m.launch_agents_dir(home)
    existing = [
        m.plist_label(svc)
        for svc in SERVICE_ORDER
        if (agents / f"{m.plist_label(svc)}.plist").exists()
    ]
    if existing:
        raise m.StackError(
            "this machine already runs Snowline services that stack sync does "
            f"not manage ({', '.join(existing)}) with no stack.json — that is "
            "the source-run hub posture, and sync is SPOKE-ONLY (decision "
            "54447516); it will not stand up a second service set here"
        )


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
            "~/.config/snowline/stack.json is not set up — --auto never "
            "prompts (missing config = report + fail, work item body); run "
            "`snowline stack sync` once interactively first"
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


def build_service_venv(
    runner: Runner,
    *,
    home: Path,
    train: str,
    manifest: m.TrainManifest,
    service: str,
    find_links: list[Path],
) -> Path:
    entry = manifest.service(service)
    sdk_entry = manifest.sdk()
    venv_dir = m.service_venv_dir(home, service, train)
    ensure_interpreter(runner)
    runner.run(["uv", "venv", "--python", "3.12", str(venv_dir)])
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


def gc_service_trains(home: Path, service: str, *, keep: set[str]) -> list[str]:
    root = m.service_dir(home, service)
    if not root.exists():
        return []
    installed = [p.name for p in root.iterdir() if p.is_dir() and p.name != "current"]
    doomed = m.trains_to_gc(installed, keep=list(keep))
    for name in doomed:
        shutil.rmtree(root / name, ignore_errors=True)
    return doomed


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
    return (_TEMPLATES_DIR / f"{service}.env.tmpl").read_text()


def env_variables(cfg: m.StackConfig, *, dashboard_dist: str) -> dict[str, str]:
    return {
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


# --------------------------------------------------------------------------
# Run report persistence.
# --------------------------------------------------------------------------


def write_report(home: Path, report: m.RunReport, *, dry_run: bool) -> None:
    if dry_run:
        return
    path = m.report_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json())
    history_dir = m.report_history_dir(home)
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.finished_at.replace(":", "").replace("+00:00", "Z")
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
) -> m.RunReport:
    started_at = _now_iso()
    resolved_train = train or "unknown"
    instance_id = "unknown"
    try:
        existing_cfg = read_stack_config(home)
        m.check_spoke_posture(requested_role=role, existing=existing_cfg)
        m.check_no_primary_env(read_existing_env_texts(home))
        check_not_source_hub(home)
        preflight_gh_auth(runner)
        cfg = ensure_stack_config(home, existing=existing_cfg, auto=auto, dry_run=dry_run, prompt=prompt)
        instance_id = cfg.instance_id

        latest = latest_platform_tag(runner) if not train else ""
        resolved_train = m.resolve_train_version(train, latest)

        current_trains = {svc: read_current_train(home, svc) for svc in SERVICE_ORDER}

        if m.is_noop(resolved_train=resolved_train, current_trains=current_trains):
            # A no-op still health-checks (#210 review): the --auto timer's
            # exit code is the monitoring signal, and an up-to-date spoke with
            # a dead gateway must not report success indefinitely. One attempt
            # only — nothing was just kickstarted, so there is no boot race to
            # wait out; a healthy stack answers immediately.
            noop_health = None if dry_run else check_health(
                runner, health_url, attempts=3, sleep=health_sleep
            )
            unhealthy = noop_health is False
            rpt = m.RunReport(
                train=resolved_train, role=cfg.role, instance_id=cfg.instance_id,
                started_at=started_at, finished_at=_now_iso(), auto=auto, dry_run=dry_run,
                noop=True, services=(), health_ok=noop_health,
                outcome="needs_attention" if unhealthy else "noop",
                detail=(
                    "already on the resolved train, but the gateway health "
                    "check FAILED — services may be down; check launchctl "
                    "and the service logs (nothing was changed by sync)"
                    if unhealthy
                    else "already on the resolved train — nothing to do"
                ),
            )
            write_report(home, rpt, dry_run=dry_run)
            report(
                f"stack sync: already on {resolved_train}, "
                + ("but the stack is UNHEALTHY" if unhealthy else "nothing to do")
            )
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
                runner, home=home, train=resolved_train, manifest=manifest, service=svc, find_links=find_links
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

        ensure_databases(runner, report)

        for i, change in enumerate(changes):
            if change.changed:
                ok = kickstart_service(runner, home, change.service)
                changes[i] = m.ServiceChange(
                    service=change.service, previous_train=change.previous_train,
                    target_train=change.target_train, changed=True,
                    migration_crossed=change.migration_crossed, kickstarted=ok,
                )

        health_ok = check_health(runner, health_url, sleep=health_sleep)
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
            health_ok = check_health(runner, health_url, sleep=health_sleep)

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

        rpt = m.RunReport(
            train=resolved_train, role=cfg.role, instance_id=cfg.instance_id,
            started_at=started_at, finished_at=_now_iso(), auto=auto, dry_run=False,
            noop=False, services=tuple(changes), health_ok=health_ok, outcome=outcome,
            detail=detail, env_drift=tuple(env_drift),
        )
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
