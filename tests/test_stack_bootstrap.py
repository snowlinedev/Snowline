"""`snowline stack bootstrap-spoke` orchestration (item 71317cd6 /
snowline-pm#122) with the replicate-CLI/curl/launchctl seam faked — the SAME
`release.runner.Runner` contract `test_stack_sync.py` fakes.

bootstrap-spoke WRAPS `snowline replicate seed`/`reseed-check` as
SUBPROCESSES (never imports `replication_seed`'s functions), so the fake
runner intercepts `["snowline", "replicate", ...]` argv exactly the way it
intercepts `["gh", ...]` / `["uv", ...]` in `test_stack_sync.py` — that is
what lets these tests assert the EXACT command chain without a real
Postgres, tailnet, or `snowline` on PATH.
"""

from __future__ import annotations

import json
from pathlib import Path

from snowline_platform.release.runner import CommandError
from snowline_platform.stack import bootstrap as b
from snowline_platform.stack import model as m
from snowline_platform.stack import sync as s


class FakeRunner:
    """Same two-method contract as `test_stack_sync.py`'s fake."""

    def __init__(self, handler=None, *, dry_run: bool = False):
        self.handler = handler or (lambda argv, cwd: "")
        self.dry_run = dry_run
        self.reads: list[tuple[str, ...]] = []
        self.runs: list[tuple[str, ...]] = []

    def read(self, argv, *, cwd=None, env=None, check=True):
        self.reads.append(tuple(argv))
        out = self.handler(list(argv), cwd)
        if isinstance(out, Exception):
            if not check:
                return ""
            raise out
        return out

    def run(self, argv, *, cwd=None, env=None, check=True):
        if self.dry_run:
            return ""
        self.runs.append(tuple(argv))
        out = self.handler(list(argv), cwd)
        if isinstance(out, Exception):
            if not check:
                return ""
            raise out
        return out if isinstance(out, str) else ""

    def ran(self, *prefix: str) -> list[tuple[str, ...]]:
        return [a for a in self.reads + self.runs if a[: len(prefix)] == prefix]


def _noop_sleep(_seconds: float) -> None:
    pass


PM_ENV_SPOKE = "export SNOWLINE_INSTANCE_ID=roam\nexport SNOWLINE_PM_ROLE=spoke\n"
PM_ENV_PRIMARY = "export SNOWLINE_INSTANCE_ID=roam\nexport SNOWLINE_PM_ROLE=primary\n"


def _install_services(home: Path, train: str = "v0.2.0") -> None:
    for svc in m.SEED_PARTICIPANTS:
        target = m.service_venv_dir(home, svc, train)
        target.mkdir(parents=True)
        s.swap_current(home, svc, target)


def _write_stack_config(home: Path, cfg: m.StackConfig) -> None:
    path = m.stack_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cfg.to_json())


def _write_pm_env(home: Path, text: str = PM_ENV_SPOKE) -> None:
    path = m.env_file_path(home, "pm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _fully_bootstrapped_home(tmp_path: Path, *, pm_env: str = PM_ENV_SPOKE) -> Path:
    home = tmp_path / "home"
    _install_services(home)
    _write_stack_config(
        home,
        m.StackConfig(
            role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net",
            primary_gateway_url="http://mini.ts.net:8850", local_tailnet_address="roam.ts.net",
        ),
    )
    _write_pm_env(home, pm_env)
    return home


def _health_ok_handler(argv, cwd):
    if argv[:1] == ["curl"]:
        return ""  # exit 0 == healthy
    return ""


def _no_prompt(_message: str) -> str:
    raise AssertionError("prompt should not be called in this test")


# -- precondition refusals, each checked in order ---------------------------


def test_refuses_when_stack_json_is_missing(tmp_path):
    home = tmp_path / "home"
    runner = FakeRunner(_health_ok_handler)
    report = b.run_bootstrap_spoke(home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep)
    assert report.outcome == "failed"
    assert "run `snowline stack sync` first" in report.detail
    assert report.steps[-1].name == "failed"
    # nothing was ever touched: no replicate/curl call happened.
    assert not runner.reads and not runner.runs


def test_refuses_when_local_services_not_installed(tmp_path):
    home = tmp_path / "home"
    _write_stack_config(
        home, m.StackConfig(role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net")
    )
    _write_pm_env(home)
    runner = FakeRunner(_health_ok_handler)
    report = b.run_bootstrap_spoke(home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep)
    assert report.outcome == "failed"
    assert "not installed yet" in report.detail
    assert "run `snowline stack sync` first" in report.detail


def test_refuses_when_local_gateway_unhealthy(tmp_path):
    home = tmp_path / "home"
    _install_services(home)
    _write_stack_config(
        home, m.StackConfig(role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net")
    )
    _write_pm_env(home)

    def handler(argv, cwd):
        if argv[:1] == ["curl"]:
            return CommandError(argv, 7, "connection refused")
        return ""

    runner = FakeRunner(handler)
    report = b.run_bootstrap_spoke(
        home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep, health_url="http://127.0.0.1:8848/health"
    )
    assert report.outcome == "failed"
    assert "local gateway is not healthy" in report.detail
    # never got to the primary or replicate at all.
    assert not runner.ran("snowline", "replicate")


def test_refuses_when_primary_gateway_unreachable(tmp_path):
    home = tmp_path / "home"
    _install_services(home)
    _write_stack_config(
        home,
        m.StackConfig(
            role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net",
            primary_gateway_url="http://mini.ts.net:8850", local_tailnet_address="roam.ts.net",
        ),
    )
    _write_pm_env(home)

    def handler(argv, cwd):
        if argv[:1] == ["curl"]:
            url = argv[-1]
            if "mini.ts.net" in url:
                return CommandError(argv, 7, "connection refused")
            return ""
        return ""

    runner = FakeRunner(handler)
    report = b.run_bootstrap_spoke(home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep)
    assert report.outcome == "failed"
    assert "primary's gateway" in report.detail
    assert "not reachable" in report.detail
    assert not runner.ran("snowline", "replicate")


def test_prompts_for_missing_primary_gateway_url_and_local_tailnet_address(tmp_path):
    home = tmp_path / "home"
    _install_services(home)
    _write_stack_config(
        home, m.StackConfig(role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net")
    )
    _write_pm_env(home)
    prompts: list[str] = []

    def prompt(message: str) -> str:
        prompts.append(message)
        if "OWN tailnet host" in message:
            return "roam.ts.net"
        return ""  # take the offered default for the gateway url

    runner = FakeRunner(_health_ok_handler)
    report = b.run_bootstrap_spoke(home=home, runner=runner, prompt=prompt, health_sleep=_noop_sleep)
    assert report.outcome == "ok"
    assert report.primary_gateway_url == "http://mini.ts.net:8850"  # the DEFAULT, port 8850
    cfg = s.read_stack_config(home)
    assert cfg.primary_gateway_url == "http://mini.ts.net:8850"
    assert cfg.local_tailnet_address == "roam.ts.net"


# -- the wrapped replicate command sequence (exact argv) --------------------


def test_fresh_bootstrap_wraps_seed_then_kickstarts_then_reverse_pairs(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)
    runner = FakeRunner(_health_ok_handler)
    report = b.run_bootstrap_spoke(home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep)

    assert report.outcome == "ok", report.detail
    seed_path = m.seed_config_path(home)

    # snowline replicate seed --config <seed.json>  (§7 steps 1-3)
    assert ("snowline", "replicate", "seed", "--config", str(seed_path)) in runner.runs
    # then boot: kickstart every service, in SEED_PARTICIPANTS order.
    kickstarted = [a for a in runner.runs if a[:2] == ("launchctl", "kickstart")]
    assert len(kickstarted) == 4
    # then §7 step 4: pair the reverse direction.
    assert (
        "snowline", "replicate", "seed", "--config", str(seed_path), "--reverse-pair",
    ) in runner.runs
    # a bare `pair` is NEVER invoked — the runbook says skip it for a fresh
    # stand-up (§4: "Skip to §5 if you are STANDING UP a spoke").
    assert not runner.ran("snowline", "replicate", "pair")
    # reseed-check is NOT invoked on a fresh (non---reseed) bootstrap.
    assert not runner.ran("snowline", "replicate", "reseed-check")

    # the seed.json actually written matches what the argv points at.
    written = json.loads(seed_path.read_text())
    assert written["primary"]["platform_url"] == "http://mini.ts.net:8850"
    assert written["spoke"]["instance"] == "roam"
    # ...and carries no primary-side Postgres URL/credential at all: the
    # primary serves its own snapshot now (item 0ebe6a70 / decision 1a83031c),
    # which is also why `_no_prompt` above holds — nothing was asked for.
    assert "primary_dump_url" not in seed_path.read_text()

    # pm role verified.
    assert report.pm_role_ok is True
    step_names = [st.name for st in report.steps]
    assert "pm-role-verified" in step_names


def test_seed_argv_order_matches_seed_before_kickstart_before_reverse_pair(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)
    runner = FakeRunner(_health_ok_handler)
    b.run_bootstrap_spoke(home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep)
    seed_path = m.seed_config_path(home)
    plain_seed = ["snowline", "replicate", "seed", "--config", str(seed_path)]
    reverse = plain_seed + ["--reverse-pair"]
    seed_i = runner.runs.index(tuple(plain_seed))
    reverse_i = runner.runs.index(tuple(reverse))
    kickstart_indices = [i for i, a in enumerate(runner.runs) if a[:2] == ("launchctl", "kickstart")]
    assert seed_i < min(kickstart_indices) < reverse_i


# -- --reseed path: reseed-check first, surfaced preconditions --------------


def test_reseed_runs_reseed_check_before_seed_reseed(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)
    runner = FakeRunner(_health_ok_handler)
    report = b.run_bootstrap_spoke(
        home=home, runner=runner, prompt=_no_prompt, reseed=True, health_sleep=_noop_sleep
    )
    assert report.outcome == "ok", report.detail
    seed_path = m.seed_config_path(home)
    assert ("snowline", "replicate", "reseed-check", "--config", str(seed_path)) in runner.reads
    seed_reseed = ("snowline", "replicate", "seed", "--config", str(seed_path), "--reseed")
    assert seed_reseed in runner.runs


def test_reseed_check_failure_surfaces_the_parked_set_precondition(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)

    def handler(argv, cwd):
        if argv[:1] == ["curl"]:
            return ""
        if argv[:3] == ["snowline", "replicate", "reseed-check"]:
            return CommandError(
                argv, 1,
                "error: re-seed preconditions NOT met (§7 step 5):\n  - roam: "
                "primary has 2 parked event(s) on the spoke's stream "
                "'roam.governance' — resolve/re-apply them before re-seeding",
            )
        return ""

    runner = FakeRunner(handler)
    report = b.run_bootstrap_spoke(
        home=home, runner=runner, prompt=_no_prompt, reseed=True, health_sleep=_noop_sleep
    )
    assert report.outcome == "failed"
    assert "parked event(s)" in report.detail
    # the mutating re-seed itself must NEVER have been attempted.
    assert not runner.ran("snowline", "replicate", "seed", "--config")
    seed_path = m.seed_config_path(home)
    assert (
        "snowline", "replicate", "seed", "--config", str(seed_path), "--reseed",
    ) not in runner.runs


# -- pm role verification ----------------------------------------------------


def test_refuses_loudly_when_pm_env_declares_a_non_spoke_role(tmp_path):
    home = _fully_bootstrapped_home(tmp_path, pm_env=PM_ENV_PRIMARY)
    runner = FakeRunner(_health_ok_handler)
    report = b.run_bootstrap_spoke(home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep)
    assert report.outcome == "failed"
    assert "SNOWLINE_PM_ROLE='primary'" in report.detail
    # the check now runs at PRECONDITION time (#212 review): the wrong
    # topology is refused BEFORE seed/kickstart/reverse-pair ever run.
    assert not runner.ran("snowline", "replicate", "seed", "--config")


def test_refuses_when_pm_env_is_missing_entirely(tmp_path):
    home = tmp_path / "home"
    _install_services(home)
    _write_stack_config(
        home,
        m.StackConfig(
            role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net",
            primary_gateway_url="http://mini.ts.net:8850", local_tailnet_address="roam.ts.net",
        ),
    )
    # no pm.env written.
    runner = FakeRunner(_health_ok_handler)
    report = b.run_bootstrap_spoke(home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep)
    assert report.outcome == "failed"
    assert "pm.env does not exist" in report.detail


# -- --dry-run: nothing mutating actually runs -------------------------------


def test_dry_run_never_seeds_kickstarts_or_pairs(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)
    runner = FakeRunner(_health_ok_handler, dry_run=True)
    # stack.json already carries both additive fields, and since item 0ebe6a70
    # there is no credential prompt at all — a dry run must ask nothing.
    report = b.run_bootstrap_spoke(
        home=home, runner=runner, prompt=_no_prompt, dry_run=True, health_sleep=_noop_sleep
    )
    assert report.outcome == "ok", report.detail
    assert report.dry_run is True
    assert not runner.runs  # every mutating call was a Runner-level no-op
    # the seed.json is NOT written under --dry-run.
    assert not m.seed_config_path(home).exists()
    step_status = {st.name: st.status for st in report.steps}
    assert step_status["seed-config-written"] == "skipped"
    assert any(name.startswith("kickstart-") and status == "skipped" for name, status in step_status.items())


# -- stack.json additive migration through the real orchestration path ------


def test_ensure_bootstrap_config_persists_prompted_fields(tmp_path):
    home = tmp_path / "home"
    cfg = m.StackConfig(role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net")
    _write_stack_config(home, cfg)

    def prompt(message: str) -> str:
        if "OWN tailnet host" in message:
            return "roam.ts.net"
        return "http://mini.ts.net:9999"  # explicit override, not the default

    updated = b.ensure_bootstrap_config(home, cfg, dry_run=False, prompt=prompt)
    assert updated.primary_gateway_url == "http://mini.ts.net:9999"
    assert updated.local_tailnet_address == "roam.ts.net"
    reloaded = s.read_stack_config(home)
    assert reloaded == updated


def test_ensure_bootstrap_config_is_a_noop_when_already_present(tmp_path):
    home = tmp_path / "home"
    cfg = m.StackConfig(
        role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net",
        primary_gateway_url="http://mini.ts.net:8850", local_tailnet_address="roam.ts.net",
    )
    result = b.ensure_bootstrap_config(home, cfg, dry_run=False, prompt=_no_prompt)
    assert result is cfg  # never prompted, never rewritten


def test_ensure_bootstrap_config_does_not_write_under_dry_run(tmp_path):
    home = tmp_path / "home"
    cfg = m.StackConfig(role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net")

    def prompt(message: str) -> str:
        return "roam.ts.net" if "OWN tailnet host" in message else ""

    b.ensure_bootstrap_config(home, cfg, dry_run=True, prompt=prompt)
    assert not m.stack_config_path(home).exists()
