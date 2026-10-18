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


def _plugins_json(names) -> str:
    """The spoke platform's `GET /plugins` body with `names` registered WITH a
    replication block (what the #228 registration gate reads)."""
    return json.dumps({
        "plugins": [
            {"name": n, "status": "up",
             "manifest": {"name": n, "base_url": f"http://127.0.0.1/{n}",
                          "replication": {"contract_version": 2,
                                          "ingest_path": "/events/ingest",
                                          "events": [f"{n}.recorded"]}}}
            for n in names
        ]
    })


ALL_PLUGINS = [svc for svc in m.SEED_PARTICIPANTS if svc != "platform"]


def _health_ok_handler(argv, cwd):
    if argv[:1] == ["curl"]:
        if argv[-1].endswith("/plugins"):
            return _plugins_json(ALL_PLUGINS)
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


# -- #228: registration gate before reverse-pair -----------------------------


def _reverse_argv(home):
    return ("snowline", "replicate", "seed", "--config", str(m.seed_config_path(home)),
            "--reverse-pair")


def test_registration_gate_waits_until_every_plugin_registers(tmp_path):
    """/health answering is not registration: the gate polls the spoke's
    /plugins until pm (the laggard) appears, and only THEN reverse-pairs."""
    home = _fully_bootstrapped_home(tmp_path)
    polls = {"n": 0}

    def handler(argv, cwd):
        if argv[:1] == ["curl"] and argv[-1].endswith("/plugins"):
            polls["n"] += 1
            names = ALL_PLUGINS if polls["n"] >= 3 else ["governance", "memory"]
            return _plugins_json(names)
        return ""

    runner = FakeRunner(handler)
    sleeps: list[float] = []
    report = b.run_bootstrap_spoke(
        home=home, runner=runner, prompt=_no_prompt, health_sleep=sleeps.append
    )
    assert report.outcome == "ok", report.detail
    assert polls["n"] == 3
    assert sleeps.count(b.REGISTRATION_POLL_S) == 2
    assert _reverse_argv(home) in runner.runs
    # the gate ran BEFORE the reverse-pair, against the spoke's own platform.
    plugins_reads = [a for a in runner.reads if a[-1].endswith("/plugins")]
    assert plugins_reads[-1][-1] == "http://127.0.0.1:8848/plugins"
    step_names = [st.name for st in report.steps]
    assert step_names.index("participants-registered") < step_names.index("reverse-pair")


def test_registration_gate_times_out_naming_the_missing_plugin_and_its_err_log(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)

    def handler(argv, cwd):
        if argv[:1] == ["curl"] and argv[-1].endswith("/plugins"):
            return _plugins_json(["governance", "memory"])  # pm never registers
        return ""

    runner = FakeRunner(handler)
    report = b.run_bootstrap_spoke(
        home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep,
        registration_timeout=6.0, registration_poll=2.0,
    )
    assert report.outcome == "failed"
    assert "pm (see " in report.detail
    assert str(m.service_err_log_path(home, "pm")) in report.detail
    assert "governance (see" not in report.detail
    # bounded: ceil(6/2)+1 polls, then refusal — and NO reverse-pair attempted.
    assert len([a for a in runner.reads if a[-1].endswith("/plugins")]) == 4
    assert _reverse_argv(home) not in runner.runs


def test_registration_gate_ignores_a_plugin_listed_without_replication(tmp_path):
    """A plugin registered WITHOUT a replication block is invisible to
    reverse-pair's discovery, so it does not satisfy the gate either."""
    home = _fully_bootstrapped_home(tmp_path)
    body = json.loads(_plugins_json(["governance", "memory"]))
    body["plugins"].append({"name": "pm", "status": "up",
                            "manifest": {"name": "pm", "base_url": "http://x", "replication": None}})

    def handler(argv, cwd):
        if argv[:1] == ["curl"] and argv[-1].endswith("/plugins"):
            return json.dumps(body)
        return ""

    runner = FakeRunner(handler)
    report = b.run_bootstrap_spoke(
        home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep,
        registration_timeout=2.0, registration_poll=2.0,
    )
    assert report.outcome == "failed"
    assert "pm (see " in report.detail


def test_registration_gate_refuses_when_plugins_never_answers(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)

    def handler(argv, cwd):
        if argv[:1] == ["curl"] and argv[-1].endswith("/plugins"):
            return CommandError(argv, 22, "HTTP 500")
        return ""

    runner = FakeRunner(handler)
    report = b.run_bootstrap_spoke(
        home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep,
        registration_timeout=2.0, registration_poll=2.0,
    )
    assert report.outcome == "failed"
    assert "/plugins never answered" in report.detail
    assert _reverse_argv(home) not in runner.runs


def test_dry_run_skips_the_registration_gate_and_probe(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)
    runner = FakeRunner(_health_ok_handler, dry_run=True)
    report = b.run_bootstrap_spoke(
        home=home, runner=runner, prompt=_no_prompt, dry_run=True, health_sleep=_noop_sleep
    )
    status = {st.name: st.status for st in report.steps}
    assert status["participants-registered"] == "skipped"
    assert status["ingest-reachability"] == "skipped"
    assert not [a for a in runner.reads if a[-1].endswith("/plugins")]


# -- #228: ingest reachability precheck (WARN, never refuse) ----------------


def test_ingest_probe_warns_on_unreachable_port_but_still_reverse_pairs(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)
    lines: list[str] = []

    def handler(argv, cwd):
        if argv[:1] == ["curl"]:
            url = argv[-1]
            if url.endswith("/plugins"):
                return _plugins_json(ALL_PLUGINS)
            if "-w" in argv and "/via/pm/" in url:
                return CommandError(argv, 7, "Failed to connect")
            if "-w" in argv and "/via/memory/" in url:
                return "502"
            if "-w" in argv:
                return "405"
        return ""

    runner = FakeRunner(handler)
    report = b.run_bootstrap_spoke(
        home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep,
        report=lines.append,
    )
    assert report.outcome == "ok", report.detail
    warns = [ln for ln in lines if ln.startswith("[ingest-reachability] WARN ")]
    assert len(warns) == 2
    pm_warn = next(w for w in warns if "pm:" in w)
    assert "roam.ts.net:8848" in pm_warn and "tailscale serve" in pm_warn
    assert any("memory:" in w and "HTTP 502" in w for w in warns)
    probe = next(st for st in report.steps if st.name == "ingest-reachability")
    assert probe.detail.startswith("WARN")
    assert _reverse_argv(home) in runner.runs  # a warning, not a refusal


def test_ingest_probe_hits_every_participants_tailnet_ingest_url(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)
    runner = FakeRunner(_health_ok_handler)
    report = b.run_bootstrap_spoke(home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep)
    assert report.outcome == "ok", report.detail
    probed = [a[-1] for a in runner.reads if a[:1] == ("curl",) and "-w" in a]
    seed_cfg = json.loads(m.seed_config_path(home).read_text())
    assert probed == [p["spoke_ingest_url"] for p in seed_cfg["participants"].values()]
    assert all(u.startswith("http://roam.ts.net:8848") for u in probed)


def test_probe_ingest_reachability_unit():
    runner = FakeRunner(lambda argv, cwd: "404")
    assert b.probe_ingest_reachability(
        runner, {"pm": {"spoke_ingest_url": "http://roam.ts.net:8848/via/pm/events/ingest"}}
    ) == []


# -- #228: a re-run reverse-pairs idempotently -------------------------------


def test_second_bootstrap_rerun_reverse_pairs_again_without_failing(tmp_path):
    """bootstrap-spoke always delegates to `replicate seed --reverse-pair`,
    which is idempotent since #228 (already-paired participants skip) — so a
    second run after fixing one plugin reaches reverse-pair and succeeds."""
    home = _fully_bootstrapped_home(tmp_path)
    for _ in range(2):
        runner = FakeRunner(_health_ok_handler)
        report = b.run_bootstrap_spoke(home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep)
        assert report.outcome == "ok", report.detail
        assert _reverse_argv(home) in runner.runs


# -- the failover front (issue #260) -----------------------------------------


def test_post_boot_health_also_probes_the_failover_front(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)
    runner = FakeRunner(_health_ok_handler)
    report = b.run_bootstrap_spoke(home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep)
    assert report.outcome == "ok", report.detail
    curls = [a[-1] for a in runner.reads if a[:1] == ("curl",)]
    assert s.FRONT_HEALTH_URL in curls
    # bootstrap does not kickstart the front — sync owns it
    kicked = [a[-1] for a in runner.runs if a[:2] == ("launchctl", "kickstart")]
    assert not any(k.endswith("/dev.snowline.front") for k in kicked)


def test_a_dead_front_is_a_warn_step_never_a_bootstrap_failure(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)

    def dead_front(argv, cwd):
        if argv[:1] == ["curl"] and argv[-1] == s.FRONT_HEALTH_URL:
            return CommandError(argv, 7, "connection refused")
        return _health_ok_handler(argv, cwd)

    report = b.run_bootstrap_spoke(
        home=home, runner=FakeRunner(dead_front), prompt=_no_prompt, health_sleep=_noop_sleep
    )
    assert report.outcome == "ok", report.detail
    steps = {st.name: st for st in report.steps}
    assert steps["post-boot-health"].status == "ok"
    assert steps["front-health"].status == "warned"
    assert steps["front-health"].detail.startswith("WARN:")


def test_bootstrap_installs_the_autoupgrade_timer_as_its_last_step(tmp_path):
    home = _fully_bootstrapped_home(tmp_path)
    runner = FakeRunner(_health_ok_handler)
    report = b.run_bootstrap_spoke(home=home, runner=runner, prompt=_no_prompt, health_sleep=_noop_sleep)
    assert report.outcome == "ok", report.detail
    assert [st.name for st in report.steps][-1] == "autoupgrade-timer"
    assert [st.name for st in report.steps].index("reverse-pair") < len(report.steps) - 1
    assert m.plist_path(home, m.AUTOUPGRADE_UNIT).exists()
    timer = [a for a in runner.runs if "dev.snowline.autoupgrade" in " ".join(a)]
    assert [a[1] for a in timer] == ["bootstrap"]  # loaded, never kickstarted
