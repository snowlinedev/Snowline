"""`snowline release autocut` — the hub's unattended patch-train cutter (#258).

The MBP spoke is a WARM STANDBY: when the hub loses power Sean keeps working
against it, so it must carry what the hub has been running, not a train that
lags by weeks. The spoke side already updates itself (`stack sync --auto`);
this is the hub side — a launchd timer (`ops/hub/launchdaemons/
dev.snowline.autocut.plist`) runs this verb hourly, and it cuts a PATCH train
whenever any component's `origin/main` has moved since the last recorded cut.

The whole decision lives here, in Python, so it is unit-testable; the plist
only runs it. One run:

  lock            (an hourly fire during a ~6-minute cut is a no-op)
  -> read release/train.json + components.json; fetch each checkout
  -> moved?       per component: does origin/main differ from the recorded
                  sha in anything but release/train.json itself (our own
                  record commit must not re-trigger a cut forever)?
                  nothing moved -> one-line report, exit 0
  -> gates        every checkout on main, clean, AT origin/main (the
                  2026-09-13 lesson: a hub behind main broke the first spoke
                  bootstrap); gh authenticated; hub /health 200 and every
                  train service registered + routable on GET /plugins; the
                  train's milestone gate answers "ok" (never skipped, never
                  forced). Any failure -> report, exit 2, NOTHING cut.
  -> version      next PATCH of the recorded train (v0.5.1 -> v0.5.2) — never
                  a minor: the minor's milestone is achieved, the next one is
                  open, so a patch is the only train a robot may cut.
  -> cut()        in-process, default options
  -> record       commit release/train.json (only it) on main + push
  -> report       autocut-report.json beside sync-report.json /
                  bootstrap-report.json, history for every non-noop run.

Never prompts. A failed cut leaves no committed record: the record commit
happens only after `cut()` returned, and `cut()` itself writes the checkout
copy only once everything published.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import cutter as c
from . import gate as g
from . import model as m
from . import runner as r
from .model import ReleaseConfig, ReleaseError

# Outcomes. Exit codes follow `stack sync --auto`'s discipline: 0 for "nothing
# to do / did it", nonzero for anything the operator should look at, with a
# distinct code for "refused before touching anything".
OUTCOME_NOOP = "noop"
OUTCOME_OK = "ok"
OUTCOME_LOCKED = "locked"
OUTCOME_GATED = "gated"
OUTCOME_FAILED = "failed"
OUTCOME_DRY_RUN = "dry_run"
# The train IS published on GitHub (tags + releases, so spokes can sync it),
# but its release/train.json record commit could not be pushed. The record is
# left uncommitted and the next run lands it; exits nonzero so it is seen.
OUTCOME_PUBLISHED_UNRECORDED = "published_unrecorded"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_GATED = 2

REPORT_NAME = "autocut-report.json"
HISTORY_DIR_NAME = "autocut-history"
LOCK_NAME = "autocut.lock"

# The health gate wants every train service `up` — or `degraded` for ONE
# reason only. `degraded` is a 2xx health with a SELF-REPORTED soft issue,
# still routed (registry.PluginStatus), and the SDK replication health block
# (sdk/replication/health.py) has exactly two causes for it:
#   - "peer X not delivering for Ns (> Ts)…" — the standby spoke is off. That
#     is the situation this timer exists to fix; refusing on it would stall
#     the train for as long as the standby is powered down. ACCEPTED.
#   - "delivery tick raised N consecutive times…" — the replication loop is
#     crash-looping on the hub. Cutting through that ships whatever broke it.
#     REFUSED, as is any reason this list does not recognise.
# Multiple reasons are joined with "; ". `--strict-health` demands `up`.
TOLERATED_DEGRADED_PREFIXES = ("peer ",)

_PATCH_RE = re.compile(r"^v0\.(\d+)\.(\d+)$")

# (url) -> (status_code, parsed json body or None). Injected in tests.
HttpGet = Callable[[str], tuple[int, object]]
CutFn = Callable[..., m.TrainPlan]


# --------------------------------------------------------------------------
# Paths (beside the spoke's sync/bootstrap reports — stack.model)
# --------------------------------------------------------------------------


def app_support_dir(home: Path) -> Path:
    from snowline_platform.stack import model as stack_model

    return stack_model.app_support_dir(home)


def report_path(home: Path) -> Path:
    return app_support_dir(home) / REPORT_NAME


def report_history_dir(home: Path) -> Path:
    return app_support_dir(home) / HISTORY_DIR_NAME


def lock_path(home: Path) -> Path:
    return app_support_dir(home) / LOCK_NAME


# --------------------------------------------------------------------------
# The run report
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentState:
    component: str
    recorded_sha: str | None
    origin_sha: str | None
    moved: bool

    def to_dict(self) -> dict:
        return {
            "component": self.component,
            "recorded_sha": self.recorded_sha,
            "origin_sha": self.origin_sha,
            "moved": self.moved,
        }


@dataclass(frozen=True)
class GateResult:
    name: str
    ok: bool
    detail: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class AutocutReport:
    started_at: str
    finished_at: str = ""
    dry_run: bool = False
    outcome: str = OUTCOME_FAILED
    detail: str | None = None
    previous_version: str | None = None
    version: str | None = None  # the train this run cut (or would cut)
    components: list[ComponentState] = field(default_factory=list)
    gates: list[GateResult] = field(default_factory=list)
    record_commit: str | None = None
    # True once cut() was CALLED for `version` (whatever came of it) — what
    # makes this run the new `last_cut`.
    cut_attempted: bool = False
    # True when this run only landed a record a previous cut left
    # uncommitted (no new cut): `last_cut` then describes THAT train.
    recovered_record: bool = False
    # The most recent run that actually cut (or tried to): carried forward
    # across noop/gated runs so the readiness surface can always answer "last
    # cut: version, sha, at, outcome" from the one latest report file.
    last_cut: dict | None = None

    def cut_summary(self) -> dict:
        return {
            "version": self.version,
            "shas": {
                cs.component: cs.origin_sha for cs in self.components
            },
            "at": self.finished_at,
            "outcome": self.outcome,
            "detail": self.detail,
        }

    def recovered_summary(self) -> dict:
        return {
            "version": self.previous_version,
            "shas": {cs.component: cs.recorded_sha for cs in self.components},
            "at": self.finished_at,
            "outcome": "recorded",
            "detail": self.detail,
        }

    def to_dict(self) -> dict:
        return {
            "kind": "autocut",
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "outcome": self.outcome,
            "detail": self.detail,
            "previous_version": self.previous_version,
            "version": self.version,
            "record_commit": self.record_commit,
            "cut_attempted": self.cut_attempted,
            "recovered_record": self.recovered_record,
            "components": [cs.to_dict() for cs in self.components],
            "gates": [gr.to_dict() for gr in self.gates],
            "last_cut": self.last_cut,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n"


def exit_code(outcome: str) -> int:
    if outcome in (OUTCOME_NOOP, OUTCOME_OK, OUTCOME_LOCKED, OUTCOME_DRY_RUN):
        return EXIT_OK
    if outcome == OUTCOME_GATED:
        return EXIT_GATED
    return EXIT_FAILED


def _previous_report(home: Path) -> dict:
    try:
        data = json.loads(report_path(home).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_report(home: Path, report: AutocutReport) -> None:
    """Same shape of persistence as `stack.sync.write_report`: the latest run
    at a fixed path, plus a timestamped history entry. History is written only
    for a run that is not a noop AND whose (outcome, detail) differs from the
    previous latest report — a hub gated "behind origin/main until someone
    deploys" would otherwise leave dozens of identical entries a day."""
    from snowline_platform.stack.sync import report_history_stamp

    if report.dry_run:
        return
    previous = _previous_report(home)
    if report.cut_attempted:
        report.last_cut = report.cut_summary()
    elif report.recovered_record:
        report.last_cut = report.recovered_summary()
    else:
        last = previous.get("last_cut")
        report.last_cut = last if isinstance(last, dict) else None
    path = report_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json())
    if report.outcome == OUTCOME_NOOP:
        return
    if (previous.get("outcome"), previous.get("detail")) == (report.outcome, report.detail):
        return
    history = report_history_dir(home)
    history.mkdir(parents=True, exist_ok=True)
    stamp = report_history_stamp(report.finished_at)
    (history / f"{stamp}-{report.version or report.outcome}.json").write_text(report.to_json())


# --------------------------------------------------------------------------
# Pure decisions
# --------------------------------------------------------------------------


def next_patch(version: str) -> str:
    """`v0.5.1` -> `v0.5.2`. Always a patch: see the module docstring."""
    match = _PATCH_RE.match(version or "")
    if not match:
        raise ReleaseError(
            f"recorded train version {version!r} is not v0.MINOR.PATCH — cannot "
            "derive the next patch; cut the next train by hand"
        )
    return f"v0.{match.group(1)}.{int(match.group(2)) + 1}"


def _version_key(version: str | None) -> tuple[int, int] | None:
    match = _PATCH_RE.match(version or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def recorded_component_shas(config: ReleaseConfig, manifest: Mapping | None) -> dict[str, str | None]:
    """component -> the sha the last train recorded for it (its services all
    share one, being one repo at one tag)."""
    entries = (manifest or {}).get("components") or {}
    out: dict[str, str | None] = {}
    for comp in config.components:
        sha = None
        for svc in comp.services:
            entry = entries.get(svc.name)
            if isinstance(entry, Mapping) and entry.get("sha"):
                sha = str(entry["sha"])
                break
        out[comp.name] = sha
    return out


def expected_plugins(config: ReleaseConfig) -> list[str]:
    """Every train SERVICE must be registered on the hub (libraries — the SDK
    — are not plugins). Derived from components.json so a component added to
    the train (musher, say) is gated without touching this file."""
    return [svc.name for _comp, svc in config.services() if svc.is_service]


# --------------------------------------------------------------------------
# Side-effecting probes (all through the runner / injected http)
# --------------------------------------------------------------------------


def _moved(runner: r.Runner, comp: m.Component, path: Path, recorded: str | None, origin: str | None) -> bool:
    if not origin:
        return False  # unresolvable origin/main: the checkout gate reports it
    if not recorded:
        return True
    if recorded == origin:
        return False
    # Anything but our own record commit counts. Without the exclusion the
    # record commit autocut itself pushes would move origin/main past the
    # recorded sha and trigger a fresh cut every hour, forever.
    argv = ["git", "diff", "--name-only", recorded, origin]
    try:
        names = runner.read(argv, cwd=path)
    except r.CommandError:
        return True  # recorded sha unknown locally: be conservative, it moved
    ignore = {str(c.MANIFEST_RELPATH)} if comp.carries_manifest else set()
    return any(line.strip() and line.strip() not in ignore for line in names.splitlines())


def _pending_record(runner: r.Runner, path: Path, working: Mapping | None) -> tuple[bool, str | None]:
    """Is the working-tree release/train.json a NEWER, uncommitted train record?

    That is what a successful `release cut` without `--commit-manifest` (or an
    autocut whose record push was rejected) leaves behind — v0.4.0's record was
    lost exactly this way. Returns (pending, problem): problem is set when the
    file differs from HEAD but is NOT a newer record (a hand edit), which the
    gates refuse rather than commit.
    """
    rel = str(c.MANIFEST_RELPATH)
    status = runner.read(["git", "status", "--porcelain", "--", rel], cwd=path).strip()
    if not status:
        return False, None
    committed_text = runner.read(["git", "show", f"HEAD:{rel}"], cwd=path, check=False)
    try:
        committed = json.loads(committed_text) if committed_text.strip() else None
    except ValueError:
        committed = None
    new_key = _version_key((working or {}).get("version"))
    old_key = _version_key((committed or {}).get("version"))
    if new_key is not None and (old_key is None or new_key > old_key):
        return True, None
    return False, (
        f"{rel} has uncommitted changes that are not a newer train record "
        f"(working {(working or {}).get('version')!r} vs committed "
        f"{(committed or {}).get('version')!r}) — resolve by hand"
    )


def _default_http_get(url: str) -> tuple[int, object]:
    import httpx

    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        resp = client.get(url)
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body


def health_gate(
    platform_url: str, plugins: list[str], http_get: HttpGet, *, strict: bool
) -> GateResult:
    import httpx

    unreachable = (OSError, httpx.HTTPError)
    try:
        code, _ = http_get(f"{platform_url}/health")
    except unreachable as exc:  # connection refused, timeout
        return GateResult("hub_health", False, f"{platform_url}/health unreachable: {exc}")
    if code != 200:
        return GateResult("hub_health", False, f"{platform_url}/health answered {code}")
    try:
        code, body = http_get(f"{platform_url}/plugins")
    except unreachable as exc:
        return GateResult("hub_health", False, f"{platform_url}/plugins unreachable: {exc}")
    if code != 200 or not isinstance(body, dict):
        return GateResult("hub_health", False, f"{platform_url}/plugins answered {code}")
    entries = {
        str(p.get("name")): (str(p.get("status")), p.get("degraded_reason"))
        for p in body.get("plugins") or []
        if isinstance(p, dict)
    }
    problems = []
    notes = []
    for name in plugins:
        if name not in entries:
            problems.append(f"{name} not registered")
            continue
        status, reason = entries[name]
        if status == "up":
            continue
        if status == "degraded" and not strict and tolerated_degraded(reason):
            notes.append(f"{name} degraded: {reason}")
            continue
        problems.append(f"{name} is {status}" + (f" ({reason})" if reason else ""))
    if problems:
        return GateResult("hub_health", False, "; ".join(problems))
    detail = "all train services registered" + (f" ({', '.join(notes)})" if notes else "")
    return GateResult("hub_health", True, detail)


def tolerated_degraded(reason: object) -> bool:
    """True only when EVERY "; "-joined degraded reason is a peer-not-
    delivering one (see TOLERATED_DEGRADED_PREFIXES). No reason, a
    crash-looping delivery tick, or anything unrecognised -> False."""
    if not isinstance(reason, str) or not reason.strip():
        return False
    if "delivery tick raised" in reason:
        return False
    parts = [part.strip() for part in reason.split("; ")]
    return all(part.startswith(TOLERATED_DEGRADED_PREFIXES) for part in parts)


def milestone_gate(version: str, fetch: g.StatusFetcher) -> GateResult:
    """Stricter than `release cut`'s: unattended, a gate that could not judge
    (pm unreachable, milestone unresolved — or SNOWLINE_PLATFORM_URL unset so
    the gate asked the wrong gateway) refuses instead of warning."""
    milestone = g.train_milestone(version)
    verdict = g.evaluate(milestone, fetch)
    if verdict.status == "ok":
        return GateResult("milestone", True, f"{milestone}: no required items open")
    if verdict.status == "skipped":
        return GateResult("milestone", False, f"{milestone}: gate could not judge ({verdict.reason})")
    return GateResult(
        "milestone", False,
        f"{milestone}: {verdict.required_remaining} required item(s) open"
        + (f" — {'; '.join(verdict.lines[:3])}" if verdict.lines else ""),
    )


def choose_version(
    runner: r.Runner,
    config: ReleaseConfig,
    checkouts: Mapping[str, Path],
    heads: Mapping[str, str],
    previous_version: str,
    *,
    attempts: int = 20,
) -> str:
    """The next patch whose tag is free on every component's origin — or
    already points at the sha we would tag (a prior run that tagged and then
    died: re-cutting the same version is the cutter's recovery path). A tag
    left at a DIFFERENT sha is skipped over rather than redefined."""
    candidate = next_patch(previous_version)
    for _ in range(attempts):
        clash = False
        for comp in config.components:
            existing = r.existing_tag_sha(runner, checkouts[comp.name], candidate)
            if existing and existing != heads.get(comp.name):
                clash = True
                break
        if not clash:
            return candidate
        candidate = next_patch(candidate)
    raise ReleaseError(f"no free patch version within {attempts} of {previous_version}")


# --------------------------------------------------------------------------
# The orchestrator
# --------------------------------------------------------------------------


class _Lock:
    """Non-blocking flock on a file under Application Support. Held for the
    whole run; released by the kernel if the process dies."""

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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def run_autocut(
    config: ReleaseConfig,
    *,
    checkouts: Mapping[str, Path],
    home: Path,
    runner: r.Runner,
    platform_url: str | None,
    dry_run: bool = False,
    commit_manifest: bool = True,
    strict_health: bool = False,
    report: r.Report = print,
    http_get: HttpGet | None = None,
    fetch_status: g.StatusFetcher | None = None,
    cut_fn: CutFn | None = None,
    out_dir: Path | None = None,
) -> AutocutReport:
    rpt = AutocutReport(started_at=_now_iso(), dry_run=dry_run)
    lock = _Lock(lock_path(home))
    if not lock.acquire():
        # Another autocut (a ~6-minute cut) holds the lock: this fire is a
        # no-op. No report write — the running one owns the report.
        rpt.outcome = OUTCOME_LOCKED
        rpt.detail = f"another autocut holds {lock.path}"
        rpt.finished_at = _now_iso()
        report(f"autocut: {rpt.detail} — skipping this fire")
        return rpt
    try:
        _run(
            rpt, config, checkouts=checkouts, home=home, runner=runner,
            platform_url=platform_url, dry_run=dry_run,
            commit_manifest=commit_manifest, strict_health=strict_health,
            report=report, http_get=http_get or _default_http_get,
            fetch_status=fetch_status or g.fetch_milestone_status,
            cut_fn=cut_fn or c.cut, out_dir=out_dir,
        )
    except c.ManifestCommitError as exc:
        # After a completed cut the train is live on GitHub — "failed" would
        # misreport it. A recovery-commit failure (no cut this run) is a
        # plain failure.
        rpt.outcome = OUTCOME_PUBLISHED_UNRECORDED if rpt.cut_attempted else OUTCOME_FAILED
        rpt.detail = str(exc)
    except ReleaseError as exc:
        rpt.outcome = OUTCOME_FAILED
        rpt.detail = str(exc)
    except Exception as exc:  # noqa: BLE001 — never die without a report
        rpt.outcome = OUTCOME_FAILED
        rpt.detail = f"unexpected {type(exc).__name__}: {exc}"
    finally:
        rpt.finished_at = _now_iso()
        try:
            write_report(home, rpt)
        finally:
            lock.release()
    report(f"autocut {rpt.finished_at}: {rpt.outcome}" + (f" — {rpt.detail}" if rpt.detail else ""))
    return rpt


def _run(
    rpt: AutocutReport,
    config: ReleaseConfig,
    *,
    checkouts: Mapping[str, Path],
    home: Path,
    runner: r.Runner,
    platform_url: str | None,
    dry_run: bool,
    commit_manifest: bool,
    strict_health: bool,
    report: r.Report,
    http_get: HttpGet,
    fetch_status: g.StatusFetcher,
    cut_fn: CutFn,
    out_dir: Path | None,
) -> None:
    manifest_comp = config.manifest_component
    manifest_checkout = checkouts[manifest_comp.name]
    manifest = m.load_manifest(manifest_checkout / c.MANIFEST_RELPATH)
    if not manifest or not manifest.get("version"):
        raise ReleaseError(
            f"no recorded train at {manifest_checkout / c.MANIFEST_RELPATH} — autocut "
            "only cuts the NEXT PATCH of an existing train; cut the first one by hand"
        )
    rpt.previous_version = str(manifest["version"])
    recorded = recorded_component_shas(config, manifest)

    # One `checkout_state` per component: it fetches origin/main, so the
    # "moved?" comparison and the checkout gate below see the same refs.
    states: dict[str, m.CheckoutState] = {}
    for comp in config.components:
        ignore = (str(c.MANIFEST_RELPATH),) if comp.carries_manifest else ()
        state = c._state(runner, comp.name, checkouts[comp.name], ignore)
        states[comp.name] = state
        moved = _moved(runner, comp, checkouts[comp.name], recorded[comp.name], state.origin_head)
        rpt.components.append(
            ComponentState(comp.name, recorded[comp.name], state.origin_head, moved)
        )

    pending, record_problem = (False, None)
    if states[manifest_comp.name].is_git:
        pending, record_problem = _pending_record(runner, manifest_checkout, manifest)
    any_moved = any(cs.moved for cs in rpt.components)
    if not any_moved and not pending and not record_problem:
        rpt.outcome = OUTCOME_NOOP
        rpt.detail = f"nothing moved since {rpt.previous_version}"
        return

    moved_names = [cs.component for cs in rpt.components if cs.moved]
    report(
        f"autocut: train {rpt.previous_version}; moved: {', '.join(moved_names) or 'none'}"
        + ("; uncommitted train record pending" if pending else "")
    )

    # -- gates: any failure refuses BEFORE anything is built or committed --
    for comp in config.components:
        state = states[comp.name]
        issues = list(m.preflight_issues(state))
        if state.exists and state.is_git and state.head and state.head != state.origin_head:
            issues.append(
                f"HEAD {state.head[:12]} is not origin/main "
                f"{(state.origin_head or '?')[:12]} — the hub is not running main; "
                "deploy (pull + restart) first"
            )
        rpt.gates.append(GateResult(f"checkout:{comp.name}", not issues, "; ".join(issues) or None))
    if record_problem:
        rpt.gates.append(GateResult("train_record", False, record_problem))
    gh_ok = r.gh_auth_ok(runner)
    rpt.gates.append(GateResult(
        "gh_auth", gh_ok,
        None if gh_ok else "gh is not authenticated (`gh auth status`) — tags, releases and the record push need it",
    ))
    if not platform_url:
        rpt.gates.append(GateResult(
            "hub_health", False,
            "SNOWLINE_PLATFORM_URL is unset — the health and milestone gates "
            "cannot reach the hub gateway",
        ))
    else:
        rpt.gates.append(health_gate(platform_url.rstrip("/"), expected_plugins(config), http_get, strict=strict_health))

    failed = [gr for gr in rpt.gates if not gr.ok]
    if not failed and any_moved:
        heads = {name: st.head or "" for name, st in states.items()}
        rpt.version = choose_version(runner, config, checkouts, heads, rpt.previous_version)
        rpt.gates.append(milestone_gate(rpt.version, fetch_status))
        failed = [gr for gr in rpt.gates if not gr.ok]
    for gr in rpt.gates:
        report(f"  {'ok' if gr.ok else 'REFUSED'} {gr.name}" + (f": {gr.detail}" if gr.detail else ""))
    if failed:
        rpt.outcome = OUTCOME_GATED
        rpt.detail = "; ".join(f"{gr.name}: {gr.detail}" for gr in failed)
        return

    if dry_run:
        plan = []
        if pending:
            plan.append(f"commit the uncommitted {c.MANIFEST_RELPATH} record")
        if rpt.version:
            plan.append(f"cut {rpt.version}")
            if commit_manifest:
                plan.append(f"commit + push {c.MANIFEST_RELPATH}")
        rpt.outcome = OUTCOME_DRY_RUN
        rpt.detail = "would " + ", then ".join(plan)
        report(f"dry run: {rpt.detail}")
        if rpt.version:
            cut_fn(
                config, rpt.version, checkouts=checkouts,
                out_dir=out_dir or manifest_checkout / ".release-build" / rpt.version,
                runner=runner, report=report, fetch_status=fetch_status,
            )
        return

    if pending:
        # Land a record a previous cut left uncommitted before cutting on top
        # of it — its sha is what the new train's carry-forward reads.
        rpt.record_commit = c.commit_train_record(
            runner, manifest_checkout, rpt.previous_version,
            message=f"release: train {rpt.previous_version} (autocut: record recovered)",
            report=report,
        )
        rpt.recovered_record = True
    if not rpt.version:
        rpt.outcome = OUTCOME_OK
        rpt.detail = f"recorded {rpt.previous_version}; nothing moved to cut"
        return

    report(f"\nautocut: cutting {rpt.version}")
    rpt.cut_attempted = True
    cut_fn(
        config, rpt.version, checkouts=checkouts,
        out_dir=out_dir or manifest_checkout / ".release-build" / rpt.version,
        runner=runner, report=report, fetch_status=fetch_status,
    )
    # Only after cut() returned — a cut that raised never reaches here, so a
    # failed cut can never leave a committed record.
    if commit_manifest:
        rpt.record_commit = c.commit_train_record(
            runner, manifest_checkout, rpt.version,
            message=f"release: train {rpt.version} (autocut)",
            report=report,
        )
    rpt.outcome = OUTCOME_OK
    rpt.detail = f"cut {rpt.version}"
