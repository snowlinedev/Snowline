"""`snowline stack bootstrap-spoke` orchestration (macOS distribution spec §6,
item 71317cd6 / snowline-pm#122).

WRAPS `snowline replicate pair`/`seed`/`reseed-check` VERBATIM — this module
never reimplements a byte of replication logic (`replication_seed.py`,
`replication_pairing.py`). It only:

- checks the local/primary preconditions docs/ops/roam-runbook.md's drill
  assumes but never states as machine-checkable (§0, §5's "with the primary
  up"), refusing loudly and BEFORE touching anything;
- builds the `seed.json` the runbook's §5 has the operator hand-edit from
  `ops/roam/seed-config.example.json`, from `stack.json` plus one
  interactively-prompted Postgres user;
- drives `snowline replicate seed [--reverse-pair|--reseed]` and
  `snowline replicate reseed-check` as SUBPROCESSES through the SAME
  `release.runner.Runner` seam `stack.sync` uses — never by importing
  `replication_seed`'s functions directly, so bootstrap-spoke's own process
  never touches Postgres or the wire itself;
- kickstarts the local services per the runbook's boot order (§2) and
  verifies the result (health, `SNOWLINE_PM_ROLE=spoke`).

Deliberately NOT fused with `sync` (spec §6: "software install and data
topology are deliberately not fused") and deliberately NEVER offers an
`--auto` mode — seeding is an operator-attended operation (replication-
continuity §7), so `prompt` is never optional here the way it is for `sync`.
"""

from __future__ import annotations

import dataclasses
import json
import os
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
    kickstart_service,
    read_current_train,
    read_stack_config,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_optional(path: Path) -> str | None:
    return path.read_text() if path.exists() else None


__all__ = [
    "ensure_bootstrap_config",
    "run_bootstrap_spoke",
    "write_bootstrap_report",
    "write_seed_config",
]


def _prompt_pg_user(prompt: PromptFn) -> str:
    default = os.environ.get("USER") or "postgres"
    raw = prompt(
        f"Postgres user on the PRIMARY for pg_dump over the tailnet "
        f"(default: {default}): "
    ).strip()
    return raw or default


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
    if not changed:
        return cfg
    updated = dataclasses.replace(
        cfg, primary_gateway_url=gateway, local_tailnet_address=local_addr
    )
    if not dry_run:
        path = m.stack_config_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated.to_json())
    return updated


def write_seed_config(home: Path, seed_dict: dict, *, dry_run: bool) -> Path:
    """Regenerated every bootstrap-spoke run (unlike the operator-owned
    `*.env` files) — it is derived wholly from `stack.json` plus a
    freshly-prompted credential, never hand-edited."""
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
    stamp = report.finished_at.replace(":", "").replace("+00:00", "Z")
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
        # -- precondition 1: stack.json exists ------------------------------
        cfg = m.check_bootstrap_stack_config(read_stack_config(home))
        instance_id = cfg.instance_id
        ok("stack-config", f"instance_id={cfg.instance_id}")

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
        pg_user = _prompt_pg_user(prompt)
        seed_dict = m.build_seed_config(
            cfg, local_platform_port=m.SERVICE_PORTS["platform"], pg_user=pg_user
        )
        seed_path = write_seed_config(home, seed_dict, dry_run=dry_run)
        if dry_run:
            skipped("seed-config-written", f"would write {seed_path}")
        else:
            ok("seed-config-written", str(seed_path))

        # -- drive `snowline replicate seed`/`reseed-check` (docs/ops/roam- --
        # -- runbook.md §5-6) — wraps them verbatim, argv only ---------------
        if reseed:
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
            for svc in m.SEED_PARTICIPANTS:
                kicked = kickstart_service(runner, home, svc)
                steps.append(
                    m.BootstrapStep(name=f"kickstart-{svc}", status="ok" if kicked else "failed")
                )
                report(f"[kickstart-{svc}] {'ok' if kicked else 'FAILED'}")

        # -- re-check local health after boot ---------------------------------
        if dry_run:
            skipped("post-boot-health", "dry-run: nothing was booted")
        else:
            health_ok = check_health(runner, health_url, sleep=health_sleep)
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

        # -- §7 step 4: pair the reverse (spoke->primary) direction -----------
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
