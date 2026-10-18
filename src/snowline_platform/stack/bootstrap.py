"""`snowline stack bootstrap-spoke` orchestration (macOS distribution spec §6,
item 71317cd6 / snowline-pm#122).

WRAPS `snowline replicate pair`/`seed`/`reseed-check` VERBATIM — this module
never reimplements a byte of replication logic (`replication_seed.py`,
`replication_pairing.py`). It only:

- checks the local/primary preconditions docs/ops/roam-runbook.md's drill
  assumes but never states as machine-checkable (§0, §5's "with the primary
  up"), refusing loudly and BEFORE touching anything;
- builds the `seed.json` the runbook's §5 has the operator hand-edit from
  `ops/roam/seed-config.example.json`, entirely from `stack.json` — no
  credential prompt: since item 0ebe6a70 the primary serves its own snapshot
  over its replication-admin surface (governance decision 1a83031c keeps the
  hub's Postgres loopback-only), so there is no primary-side Postgres user;
- drives `snowline replicate seed [--reverse-pair|--reseed]` and
  `snowline replicate reseed-check` as SUBPROCESSES through the SAME
  `release.runner.Runner` seam `stack.sync` uses — never by importing
  `replication_seed`'s functions directly, so bootstrap-spoke's own process
  never touches Postgres or the wire itself;
- kickstarts the local services per the runbook's boot order (§2) and
  verifies the result (health, every plugin REGISTERED with replication on
  the spoke platform before reverse-pair — #228 — and
  `SNOWLINE_PM_ROLE=spoke`).

Deliberately NOT fused with `sync` (spec §6: "software install and data
topology are deliberately not fused") and deliberately NEVER offers an
`--auto` mode — seeding is an operator-attended operation (replication-
continuity §7), so `prompt` is never optional here the way it is for `sync`.
"""

from __future__ import annotations

import dataclasses
import json
import math
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..release.runner import CommandError, Runner
from . import model as m
from .sync import (
    DEFAULT_HEALTH_URL,
    PromptFn,
    Report,
    check_health,
    check_stack_health,
    kickstart_service,
    read_current_train,
    read_stack_config,
    report_history_stamp,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_optional(path: Path) -> str | None:
    return path.read_text() if path.exists() else None


# The registration gate (#228): how long to wait, after post-boot-health,
# for every plugin's registration heartbeat to land in the spoke platform's
# `/plugins`, and how often to look. A plugin's first beat is a task started
# in its lifespan, so it trails `/health` by moments on a good boot; a minute
# is "it is not coming", not a real bound.
REGISTRATION_TIMEOUT_S = 60.0
REGISTRATION_POLL_S = 2.0

# HTTP statuses from the spoke's gateway that mean "the gateway answered but
# the plugin behind `/via/<name>` did not" — reachable port, dead upstream.
_UPSTREAM_DOWN_CODES = frozenset({"502", "503", "504"})


__all__ = [
    "REGISTRATION_POLL_S",
    "REGISTRATION_TIMEOUT_S",
    "ensure_bootstrap_config",
    "probe_ingest_reachability",
    "registered_replicating_plugins",
    "run_bootstrap_spoke",
    "wait_for_registration",
    "write_bootstrap_report",
    "write_seed_config",
]


def ensure_bootstrap_config(
    home: Path, cfg: m.StackConfig, *, dry_run: bool, prompt: PromptFn
) -> m.StackConfig:
    """Fill the two additive stack.json fields bootstrap-spoke needs
    (`primary_gateway_url`, `local_tailnet_address`) if either is missing —
    a stack.json from before this item, or one written by `sync` alone, has
    neither. Prompted once and persisted, same posture as `sync`'s own
    `ensure_stack_config` for the original three fields."""
    gateway = cfg.primary_gateway_url
    local_addr = cfg.local_tailnet_address
    changed = False
    if not gateway:
        default = m.default_primary_gateway_url(cfg.primary_tailnet_address)
        raw = prompt(f"the primary's full gateway URL (default: {default}): ").strip()
        gateway = raw or default
        changed = True
    if not local_addr:
        raw = prompt(
            "this machine's OWN tailnet host/IP (e.g. roam.tailnet-name.ts.net "
            "— the primary dials this to deliver seeded events to): "
        ).strip()
        if not raw:
            raise m.StackError(
                "bootstrap-spoke needs this machine's own tailnet address to "
                "seed from — it has no safe default"
            )
        local_addr = raw
        changed = True
    # Validate WHATEVER we ended up with — prompted now or persisted by an
    # earlier run (#212 review: the preceding prompt asks for a full URL,
    # inviting a pasted 'http://…' here; the primary persists the resulting
    # malformed ingest URL silently and the spoke never converges).
    if "://" in local_addr or "/" in local_addr or " " in local_addr:
        raise m.StackError(
            f"local_tailnet_address must be a bare host or IP (e.g. "
            f"roam.tailnet-name.ts.net), not a URL — got {local_addr!r}; fix "
            f"it in {m.stack_config_path(home)} if it was persisted"
        )
    if not changed:
        return cfg
    updated = dataclasses.replace(
        cfg, primary_gateway_url=gateway, local_tailnet_address=local_addr
    )
    if not dry_run:
        # ADDITIVE merge into the raw document (#212 review: a parse->to_json
        # round-trip silently dropped unknown keys and stamped
        # schema_version back to 1 — this is the operator's file).
        path = m.stack_config_path(home)
        try:
            raw_doc = json.loads(path.read_text())
        except (OSError, ValueError):
            raw_doc = {}
        raw_doc.setdefault("schema_version", 1)
        raw_doc["primary_gateway_url"] = gateway
        raw_doc["local_tailnet_address"] = local_addr
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw_doc, indent=2) + "\n")
    return updated


def registered_replicating_plugins(runner: Runner, platform_url: str) -> set[str] | None:
    """Names of the plugins the platform at `platform_url` lists in `/plugins`
    WITH a `replication` block — the exact set `discover_participants` (and so
    `replicate seed --reverse-pair`) will see. `None` when `/plugins` did not
    answer or did not parse."""
    url = platform_url.rstrip("/") + "/plugins"
    try:
        out = runner.read(["curl", "-fsS", "--max-time", "5", url])
    except CommandError:
        return None
    try:
        doc = json.loads(out)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    names: set[str] = set()
    for entry in doc.get("plugins") or []:
        manifest = (entry or {}).get("manifest") or {}
        if manifest.get("replication") and manifest.get("name"):
            names.add(manifest["name"])
    return names


def wait_for_registration(
    runner: Runner,
    platform_url: str,
    expected: list[str],
    *,
    timeout: float = REGISTRATION_TIMEOUT_S,
    poll: float = REGISTRATION_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
) -> set[str] | None:
    """Poll the spoke's `/plugins` until every name in `expected` is
    registered with replication, or `timeout` runs out (bounded by attempt
    count, `ceil(timeout / poll) + 1`, so a fake sleep makes it instant in
    tests). Returns the LAST observed registered set (`None` if `/plugins`
    never answered) — the caller decides whether that is a refusal."""
    attempts = max(1, math.ceil(timeout / poll) + 1) if poll > 0 else 1
    seen: set[str] | None = None
    for i in range(attempts):
        got = registered_replicating_plugins(runner, platform_url)
        if got is not None:
            seen = got
            if all(name in got for name in expected):
                return got
        if i + 1 < attempts:
            sleep(poll)
    return seen


def probe_ingest_reachability(runner: Runner, participants: dict) -> list[str]:
    """Best-effort precheck (#228) before reverse-pair: probe each
    participant's `spoke_ingest_url` (the address the PRIMARY delivers to —
    this machine's tailnet host + the gateway port, `/via/<plugin>` for
    plugins) with one bounded HTTP request. Any HTTP answer counts as
    reachable (the ingest route is POST-only, so a GET's 404/405 is fine),
    EXCEPT a gateway 502/503/504, which means the plugin behind `/via` is not
    answering. Returns one WARN line per problem; never refuses — this box's
    view of its own tailnet address is a proxy for the primary's, not proof."""
    warnings: list[str] = []
    for name, pcfg in participants.items():
        url = pcfg.get("spoke_ingest_url")
        if not url:
            continue
        hostport = url.split("://", 1)[-1].split("/", 1)[0]
        try:
            code = runner.read(
                [
                    "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                    "--max-time", "5", url,
                ]
            ).strip()
        except CommandError:
            warnings.append(
                f"{name}: ingest {url} unreachable ({hostport} did not answer) "
                "— the primary will not be able to deliver to it. Likely "
                "cause: a `tailscale serve` mirror that predates this "
                "plugin/port (`stack sync` does not manage it; check "
                "`tailscale serve status`), or tailscaled down"
            )
            continue
        if code in _UPSTREAM_DOWN_CODES:
            warnings.append(
                f"{name}: ingest {url} answered HTTP {code} — {hostport} is "
                f"reachable but the {name} service behind it is not answering"
            )
    return warnings


def write_seed_config(home: Path, seed_dict: dict, *, dry_run: bool) -> Path:
    """Regenerated every bootstrap-spoke run (unlike the operator-owned
    `*.env` files) — it is derived wholly from `stack.json`, carries no
    credential at all (item 0ebe6a70), and is never hand-edited."""
    path = m.seed_config_path(home)
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seed_dict, indent=2, sort_keys=False) + "\n")
    return path


def write_bootstrap_report(home: Path, report: m.BootstrapReport, *, dry_run: bool) -> None:
    if dry_run:
        return
    path = m.bootstrap_report_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json())
    history_dir = m.bootstrap_report_history_dir(home)
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = report_history_stamp(report.finished_at)
    suffix = "reseed" if report.reseed else "seed"
    (history_dir / f"{stamp}-{suffix}.json").write_text(report.to_json())


def run_bootstrap_spoke(
    *,
    home: Path,
    dry_run: bool = False,
    reseed: bool = False,
    runner: Runner,
    prompt: PromptFn,
    health_url: str = DEFAULT_HEALTH_URL,
    health_sleep: Callable[[float], None] = time.sleep,
    report: Report = print,
    registration_timeout: float = REGISTRATION_TIMEOUT_S,
    registration_poll: float = REGISTRATION_POLL_S,
) -> m.BootstrapReport:
    started_at = _now_iso()
    steps: list[m.BootstrapStep] = []
    instance_id = "unknown"
    primary_gateway_url: str | None = None
    pm_role_ok: bool | None = None
    health_ok: bool | None = None

    def ok(name: str, detail: str | None = None) -> None:
        steps.append(m.BootstrapStep(name=name, status="ok", detail=detail))
        report(f"[{name}] " + (detail if detail else "ok"))

    def skipped(name: str, detail: str) -> None:
        steps.append(m.BootstrapStep(name=name, status="skipped", detail=detail))
        report(f"[{name}] skipped: {detail}")

    try:
        # -- precondition 1: stack.json exists AND declares role=spoke -------
        # (#212 review: sync's spoke-only guard, decision 54447516, must hold
        # here too — a hand-edited role=primary stack.json was previously
        # seeded and reverse-paired as a spoke.)
        cfg = m.check_bootstrap_stack_config(read_stack_config(home))
        m.check_spoke_posture(requested_role="spoke", existing=cfg)
        instance_id = cfg.instance_id
        ok("stack-config", f"instance_id={cfg.instance_id} role={cfg.role}")

        # -- precondition 1b: pm.env declares SNOWLINE_PM_ROLE=spoke ---------
        # Checked BEFORE any mutation (#212 review: this guard previously ran
        # LAST — after seed, kickstart, and reverse-pair — so the wrong
        # topology it refuses had already been stood up). pm.env is not
        # touched by any bootstrap step, so checking here loses nothing; the
        # post-pair re-verify below still confirms it end-of-run.
        m.check_pm_role_is_spoke(_read_optional(m.env_file_path(home, "pm")))
        ok("pm-role-precheck", "pm.env declares SNOWLINE_PM_ROLE=spoke")

        # -- precondition 2: local services installed -----------------------
        current_trains = {svc: read_current_train(home, svc) for svc in m.SEED_PARTICIPANTS}
        m.check_local_services_installed(current_trains)
        ok(
            "local-services-installed",
            ", ".join(f"{s}={t}" for s, t in current_trains.items()),
        )

        # -- precondition 3: local gateway healthy ---------------------------
        local_health = check_health(runner, health_url, attempts=3, sleep=health_sleep)
        m.check_local_gateway_healthy(local_health)
        ok("local-gateway-healthy")

        # -- resolve the two additive stack.json fields (prompt if missing) --
        cfg = ensure_bootstrap_config(home, cfg, dry_run=dry_run, prompt=prompt)
        primary_gateway_url = cfg.primary_gateway_url
        ok(
            "primary-gateway-resolved",
            f"primary_gateway_url={cfg.primary_gateway_url} "
            f"local_tailnet_address={cfg.local_tailnet_address}",
        )

        # -- precondition 4: primary reachable + healthy over the tailnet ----
        assert cfg.primary_gateway_url is not None  # ensure_bootstrap_config guarantees this
        primary_health_url = cfg.primary_gateway_url.rstrip("/") + "/health"
        primary_health = check_health(runner, primary_health_url, attempts=5, sleep=health_sleep)
        m.check_primary_gateway_healthy(primary_health, cfg.primary_gateway_url)
        ok("primary-gateway-healthy")

        # -- build + write the seed config -----------------------------------
        # No credential prompt: the primary serves its own snapshot over its
        # replication-admin surface (item 0ebe6a70 / decision 1a83031c), so
        # there is no primary-side Postgres user to ask for.
        seed_dict = m.build_seed_config(
            cfg, local_platform_port=m.SERVICE_PORTS["platform"]
        )
        seed_path = write_seed_config(home, seed_dict, dry_run=dry_run)
        if dry_run:
            skipped("seed-config-written", f"would write {seed_path}")
        else:
            ok("seed-config-written", str(seed_path))

        # -- drive `snowline replicate seed`/`reseed-check` (docs/ops/roam- --
        # -- runbook.md §5-6) — wraps them verbatim, argv only ---------------
        if reseed:
            if dry_run:
                # `Runner.read` executes even under --dry-run, and the dry run
                # never wrote seed.json — running the real check would crash
                # on a fresh machine or validate a STALE config (#212 review).
                skipped(
                    "reseed-check",
                    "would run: " + " ".join(m.reseed_check_cli_argv(seed_path)),
                )
            else:
                report(
                    "re-seed: checking §7 step-5 preconditions before touching "
                    "state (`snowline replicate reseed-check`)"
                )
                runner.read(m.reseed_check_cli_argv(seed_path))
                ok(
                    "reseed-check",
                    "preconditions met: spoke outbox drained AND primary parked "
                    "set empty for the spoke's streams (§7 step 5)",
                )
            runner.run(m.seed_cli_argv(seed_path, reseed=True))
            ok(
                "seed",
                "re-seed under a fresh epoch: retired old streams, then "
                "primed, dumped, scrubbed + injected (§7 steps 1-3)"
                if not dry_run
                else "would re-seed under a fresh epoch",
            )
        else:
            runner.run(m.seed_cli_argv(seed_path))
            ok(
                "seed",
                "primed, dumped, scrubbed + injected (§7 steps 1-3)"
                if not dry_run
                else "would prime, dump, scrub + inject (§7 steps 1-3)",
            )

        # -- boot the spoke (runbook §2's order) ------------------------------
        if dry_run:
            for svc in m.SEED_PARTICIPANTS:
                skipped(f"kickstart-{svc}", "dry-run: nothing was seeded to boot against")
        else:
            kick_failed: list[str] = []
            for svc in m.SEED_PARTICIPANTS:
                kicked = kickstart_service(runner, home, svc)
                steps.append(
                    m.BootstrapStep(name=f"kickstart-{svc}", status="ok" if kicked else "failed")
                )
                report(f"[kickstart-{svc}] {'ok' if kicked else 'FAILED'}")
                if not kicked:
                    kick_failed.append(svc)
            if kick_failed:
                # A failed step must fail the RUN (#212 review: a dead pm was
                # previously reverse-paired anyway and the CLI exited 0).
                raise m.StackError(
                    f"kickstart failed for: {', '.join(kick_failed)} — refusing "
                    "to reverse-pair against dead ingest endpoints; check the "
                    "plists (launchctl print) and the service logs, then re-run"
                )

        # -- re-check local health after boot ---------------------------------
        if dry_run:
            skipped("post-boot-health", "dry-run: nothing was booted")
        else:
            # the gateway AND the failover front's own /front/health (#260)
            health_ok = check_stack_health(runner, health_url, sleep=health_sleep)
            steps.append(
                m.BootstrapStep(name="post-boot-health", status="ok" if health_ok else "failed")
            )
            report(f"[post-boot-health] {'ok' if health_ok else 'FAILED'}")
            if not health_ok:
                raise m.StackError(
                    "the spoke did not come up healthy after seeding + "
                    "kickstart — check launchctl and the service logs before "
                    "re-pairing (docs/ops/roam-runbook.md 'packaged spoke' "
                    "section, 'seed interrupted/restarted')"
                )

        # -- registration gate (#228): /health answering != registered ------
        # `--reverse-pair` discovers participants from the spoke platform's
        # `/plugins`, which a plugin populates only from its registration
        # heartbeat's first beat — so wait for every plugin to be listed with
        # a replication block BEFORE pairing, and refuse naming the laggard.
        if dry_run:
            skipped("participants-registered", "dry-run: nothing was booted")
        else:
            expected = [svc for svc in m.SEED_PARTICIPANTS if svc != "platform"]
            spoke_platform_url = seed_dict["spoke"]["platform_url"]
            registered = wait_for_registration(
                runner,
                spoke_platform_url,
                expected,
                timeout=registration_timeout,
                poll=registration_poll,
                sleep=health_sleep,
            )
            m.check_participants_registered(
                home,
                registered,
                spoke_platform_url=spoke_platform_url,
                waited_s=registration_timeout,
            )
            ok("participants-registered", ", ".join(expected))

        # -- ingest reachability precheck (#228): WARN, never refuse ----------
        if dry_run:
            skipped("ingest-reachability", "dry-run: nothing was booted")
        else:
            warnings = probe_ingest_reachability(runner, seed_dict["participants"])
            for line in warnings:
                report(f"[ingest-reachability] WARN {line}")
            ok(
                "ingest-reachability",
                "WARN: " + " | ".join(warnings)
                if warnings
                else "every spoke ingest URL answered",
            )

        # -- §7 step 4: pair the reverse (spoke->primary) direction -----------
        # Idempotent since #228: already-paired participants are skipped, so a
        # re-run after fixing one plugin does not fail on the others.
        runner.run(m.seed_cli_argv(seed_path, reverse_pair=True))
        ok(
            "reverse-pair",
            "spoke->primary direction paired (§7 step 4); the spoke now "
            "converges by events alone"
            if not dry_run
            else "would pair the reverse (spoke->primary) direction",
        )

        # -- verify SNOWLINE_PM_ROLE=spoke on the RENDERED pm.env -------------
        pm_env_text = _read_optional(m.env_file_path(home, "pm"))
        pm_role_ok = False
        m.check_pm_role_is_spoke(pm_env_text)
        pm_role_ok = True
        ok("pm-role-verified", "pm.env declares SNOWLINE_PM_ROLE=spoke")

        outcome = "ok"
        detail = (
            "dry run: nothing seeded, booted, or paired — see the steps "
            "above for what a real run would do"
            if dry_run
            else "spoke bootstrapped: seeded, booted, reverse-paired, pm "
            "verified as a spoke"
        )
        rpt = m.BootstrapReport(
            instance_id=instance_id,
            primary_gateway_url=primary_gateway_url,
            started_at=started_at,
            finished_at=_now_iso(),
            dry_run=dry_run,
            reseed=reseed,
            steps=tuple(steps),
            pm_role_ok=pm_role_ok,
            health_ok=health_ok,
            outcome=outcome,
            detail=detail,
        )
        write_bootstrap_report(home, rpt, dry_run=dry_run)
        return rpt

    except (m.StackError, CommandError) as exc:
        steps.append(m.BootstrapStep(name="failed", status="failed", detail=str(exc)))
        rpt = m.BootstrapReport(
            instance_id=instance_id,
            primary_gateway_url=primary_gateway_url,
            started_at=started_at,
            finished_at=_now_iso(),
            dry_run=dry_run,
            reseed=reseed,
            steps=tuple(steps),
            pm_role_ok=pm_role_ok,
            health_ok=health_ok,
            outcome="failed",
            detail=str(exc),
        )
        write_bootstrap_report(home, rpt, dry_run=dry_run)
        return rpt
