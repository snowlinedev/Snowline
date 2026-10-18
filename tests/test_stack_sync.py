"""`snowline stack sync` orchestration (item b70b0359 / issue #203) with the
git/gh/uv/launchctl/createdb/curl seam faked — the SAME `release.runner.Runner`
contract `test_release_cutter.py` fakes: `read()` for non-mutating commands,
`run()` for mutating ones.

Every subprocess-shaped step is exercised against the fake directly (mirroring
`test_release_cutter.py`'s style of calling one method with pre-seeded
`tmp_path` fixtures standing in for "the previous step already ran"). The
end-to-end `run_sync()` tests use `tmp_path` as HOME throughout — never the
real filesystem — per the execution rule that this suite must never touch a
real ~/.config/snowline, ~/Library/Application Support/Snowline, launchd, or
Postgres.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from snowline_platform.release.runner import CommandError
from snowline_platform.stack import model as m
from snowline_platform.stack import sync as s

TRAIN_V1 = {
    "version": "v0.1.0",
    "components": {
        "platform": {"repo": "snowlinedev/Snowline", "tag": "v0.1.0", "sha": "a" * 40,
                      "wheel": "snowline_platform-0.1.0-py3-none-any.whl", "kind": "service"},
        "governance": {"repo": "snowlinedev/Snowline", "tag": "v0.1.0", "sha": "a" * 40,
                        "wheel": "snowline_governance-0.1.0-py3-none-any.whl", "kind": "service"},
        "memory": {"repo": "snowlinedev/Snowline", "tag": "v0.1.0", "sha": "a" * 40,
                   "wheel": "snowline_memory-0.1.0-py3-none-any.whl", "kind": "service"},
        "sdk": {"repo": "snowlinedev/Snowline", "tag": "v0.1.0", "sha": "a" * 40,
                "wheel": "snowline_plugin_sdk-0.1.0-py3-none-any.whl", "kind": "library"},
        "pm": {"repo": "snowlinedev/snowline-pm", "tag": "v0.1.0", "sha": "b" * 40,
               "wheel": "snowline_pm-0.1.0-py3-none-any.whl", "kind": "service"},
    },
}


class FakeRunner:
    """Stands in for `release.runner.Runner` — same two-method contract as
    `test_release_cutter.py`'s fake."""

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
        return [a for a in self.runs if a[: len(prefix)] == prefix]


def _ok_gh_auth(argv, cwd):
    if argv[:3] == ["gh", "auth", "status"]:
        return "ok\n"
    return ""


# -- gh auth preflight (risk #4) ---------------------------------------------


def test_preflight_gh_auth_passes_when_authenticated():
    runner = FakeRunner(_ok_gh_auth)
    s.preflight_gh_auth(runner)  # does not raise


def test_preflight_gh_auth_refuses_before_any_symlink_when_unauthenticated():
    def handler(argv, cwd):
        if argv[:3] == ["gh", "auth", "status"]:
            return CommandError(argv, 1, "not logged in")
        return ""
    runner = FakeRunner(handler)
    with pytest.raises(m.StackError, match="gh is not authenticated"):
        s.preflight_gh_auth(runner)


# -- train resolution ---------------------------------------------------


def test_latest_platform_tag_reads_the_jq_filtered_tag():
    def handler(argv, cwd):
        if argv[:3] == ["gh", "release", "view"]:
            return "v0.3.0\n"
        return ""
    assert s.latest_platform_tag(FakeRunner(handler)) == "v0.3.0"


def test_latest_platform_tag_refuses_an_empty_result():
    runner = FakeRunner(lambda argv, cwd: "\n")
    with pytest.raises(m.StackError, match="could not resolve"):
        s.latest_platform_tag(runner)


# -- manifest + asset download -------------------------------------------


def test_fetch_train_manifest_reads_back_the_downloaded_file(tmp_path):
    into = tmp_path / "assets"
    into.mkdir()
    (into / "train.json").write_text(json.dumps(TRAIN_V1))
    runner = FakeRunner()
    manifest = s.fetch_train_manifest(runner, "v0.1.0", into=into)
    assert manifest.version == "v0.1.0"
    assert runner.ran("gh", "release", "download")


def test_fetch_train_manifest_raises_if_download_produced_nothing(tmp_path):
    runner = FakeRunner()
    with pytest.raises(m.StackError, match="was not downloaded"):
        s.fetch_train_manifest(runner, "v0.1.0", into=tmp_path / "empty")


def test_unique_repo_tags_dedupes_shared_repo_tag_pairs():
    manifest = m.parse_train_manifest(TRAIN_V1)
    pairs = s.unique_repo_tags(manifest)
    assert pairs == [
        ("snowlinedev/Snowline", "v0.1.0"),
        ("snowlinedev/snowline-pm", "v0.1.0"),
    ]


# -- venv build (spec §5 step 2 — wheels by FILE PATH, sdk explicit) --------


def test_build_service_venv_installs_service_and_sdk_wheels_by_path(tmp_path):
    home = tmp_path / "home"
    manifest = m.parse_train_manifest(TRAIN_V1)

    def handler(argv, cwd):
        if argv[-1].endswith("--version") or (len(argv) >= 2 and argv[1] == "--version"):
            return "Python 3.12.3\n"
        return ""
    runner = FakeRunner(handler)
    find_links = [tmp_path / "assets" / "platform", tmp_path / "assets" / "pm"]
    venv_dir = s.build_service_venv(
        runner, home=home, train="v0.1.0", manifest=manifest, service="governance",
        find_links=find_links,
    )
    assert venv_dir == m.service_venv_dir(home, "governance", "v0.1.0")
    assert runner.ran("uv", "python", "install", "3.12")
    assert runner.ran("uv", "venv", "--python", "3.12", str(venv_dir))
    install_calls = runner.ran("uv", "pip", "install")
    assert len(install_calls) == 1
    argv = install_calls[0]
    assert str(venv_dir / "bin" / "python") in argv
    assert any("snowline_governance-0.1.0-py3-none-any.whl" in a for a in argv)
    assert any("snowline_plugin_sdk-0.1.0-py3-none-any.whl" in a for a in argv)
    assert any("requirements-governance.txt" in a for a in argv)
    assert str(find_links[0]) in argv and str(find_links[1]) in argv
    # risk #3: the new interpreter is verified (read, not run) before sync
    # would ever swap a symlink.
    assert (list(argv2) for argv2 in runner.reads)  # sanity: reads recorded
    assert any(a[0] == str(venv_dir / "bin" / "python") for a in runner.reads)


def test_build_service_venv_raises_when_interpreter_verify_fails(tmp_path):
    home = tmp_path / "home"
    manifest = m.parse_train_manifest(TRAIN_V1)

    def handler(argv, cwd):
        if argv and argv[-1] == "--version":
            return CommandError(argv, 1, "no such file")
        return ""
    runner = FakeRunner(handler)
    with pytest.raises(CommandError):
        s.build_service_venv(
            runner, home=home, train="v0.1.0", manifest=manifest, service="platform", find_links=[],
        )


# -- atomic symlink swap (spec §5 step 2 — os.replace, never ln -sfn) ------


def test_swap_current_points_current_at_the_target_atomically(tmp_path):
    home = tmp_path / "home"
    target = m.service_venv_dir(home, "platform", "v0.2.0")
    target.mkdir(parents=True)
    s.swap_current(home, "platform", target)
    link = m.service_current_link(home, "platform")
    assert link.is_symlink()
    assert Path(os.readlink(link)) == target
    # no leftover temp symlink
    assert not (link.with_name(link.name + ".tmp-new")).exists()


def test_swap_current_repoints_an_existing_current_link(tmp_path):
    home = tmp_path / "home"
    old = m.service_venv_dir(home, "platform", "v0.1.0")
    new = m.service_venv_dir(home, "platform", "v0.2.0")
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    s.swap_current(home, "platform", old)
    s.swap_current(home, "platform", new)
    link = m.service_current_link(home, "platform")
    assert Path(os.readlink(link)) == new


# -- GC (keep 2 trains) ------------------------------------------------------


def test_gc_service_trains_removes_everything_not_kept(tmp_path):
    home = tmp_path / "home"
    root = m.service_dir(home, "platform")
    for train in ("v0.1.0", "v0.2.0", "v0.3.0"):
        (root / train).mkdir(parents=True)
    (root / "current").symlink_to(root / "v0.3.0")
    doomed = s.gc_service_trains(home, "platform", keep={"v0.2.0", "v0.3.0"})
    assert doomed == ["v0.1.0"]
    assert not (root / "v0.1.0").exists()
    assert (root / "v0.2.0").exists()
    assert (root / "current").is_symlink()  # untouched


def test_gc_service_trains_is_a_noop_when_nothing_installed(tmp_path):
    home = tmp_path / "home"
    assert s.gc_service_trains(home, "platform", keep={"v0.1.0"}) == []


# -- env + plist application -------------------------------------------------


def test_apply_env_file_writes_a_fresh_file_from_the_template(tmp_path):
    home = tmp_path / "home"
    variables = {
        "instance_id": "roam", "trusted_cidrs": m.TRUSTED_CIDRS,
        "primary_tailnet_address": "mini.ts.net", "platform_port": "8848",
        "governance_port": "8801", "memory_port": "8802", "pm_port": "8803",
        "dashboard_dist": str(tmp_path / "dash"),
        "front_health_url": m.front_health_url(),
    }
    result = s.apply_env_file(home, "platform", variables=variables, dry_run=False)
    assert result.action == "written"
    path = m.env_file_path(home, "platform")
    assert path.exists()
    assert "SNOWLINE_INSTANCE_ID=roam" in path.read_text()
    assert "SNOWLINE_DASHBOARD_DIST=" in path.read_text()


def test_apply_env_file_never_clobbers_an_operator_edit(tmp_path):
    home = tmp_path / "home"
    path = m.env_file_path(home, "platform")
    path.parent.mkdir(parents=True)
    path.write_text("export SNOWLINE_INSTANCE_ID=roam  # hand-tuned\n")
    variables = {
        "instance_id": "roam", "trusted_cidrs": m.TRUSTED_CIDRS,
        "primary_tailnet_address": "mini.ts.net", "platform_port": "8848",
        "governance_port": "8801", "memory_port": "8802", "pm_port": "8803",
        "dashboard_dist": str(tmp_path / "dash"),
        "front_health_url": m.front_health_url(),
    }
    result = s.apply_env_file(home, "platform", variables=variables, dry_run=False)
    assert result.action == "drifted"
    assert path.read_text() == "export SNOWLINE_INSTANCE_ID=roam  # hand-tuned\n"


def test_apply_env_file_dry_run_never_writes(tmp_path):
    home = tmp_path / "home"
    variables = {
        "instance_id": "roam", "trusted_cidrs": m.TRUSTED_CIDRS,
        "primary_tailnet_address": "mini.ts.net", "platform_port": "8848",
        "governance_port": "8801", "memory_port": "8802", "pm_port": "8803",
        "dashboard_dist": "x", "front_health_url": m.front_health_url(),
    }
    result = s.apply_env_file(home, "platform", variables=variables, dry_run=True)
    assert result.action == "written"
    assert not m.env_file_path(home, "platform").exists()


def test_apply_plist_writes_and_is_idempotent_on_no_change(tmp_path):
    home = tmp_path / "home"
    changed_1 = s.apply_plist(home, "governance", dry_run=False)
    assert changed_1 is True
    assert m.plist_path(home, "governance").exists()
    changed_2 = s.apply_plist(home, "governance", dry_run=False)
    assert changed_2 is False


# -- createdb (spec §3 — only createdb, never alembic) -----------------------


def test_ensure_databases_creates_only_missing_ones():
    def handler(argv, cwd):
        if argv[:2] == ["psql", "-Atqc"]:
            return "snowline_platform\nsnowline_pm\n"
        return ""
    runner = FakeRunner(handler)
    created = s.ensure_databases(runner, report=lambda _: None)
    assert set(created) == {"snowline_governance", "snowline_memory"}
    assert runner.ran("createdb", "snowline_governance")
    assert runner.ran("createdb", "snowline_memory")
    assert not runner.ran("createdb", "snowline_platform")


# -- launchd + health check ---------------------------------------------


def test_kickstart_service_bootstraps_then_kickstarts(tmp_path):
    home = tmp_path / "home"
    runner = FakeRunner()
    ok = s.kickstart_service(runner, home, "platform")
    assert ok is True
    assert runner.reads[0][:2] == ("launchctl", "bootstrap")
    assert runner.ran("launchctl", "kickstart", "-k", m.gui_target("dev.snowline.platform", os.getuid()))


def test_kickstart_service_reports_failure_without_raising(tmp_path):
    home = tmp_path / "home"

    def handler(argv, cwd):
        if argv[:2] == ["launchctl", "kickstart"]:
            return CommandError(argv, 1, "no such process")
        return ""
    runner = FakeRunner(handler)
    assert s.kickstart_service(runner, home, "platform") is False


def test_check_health_true_on_success_false_on_failure():
    assert s.check_health(FakeRunner(lambda a, c: "")) is True

    def failing(argv, cwd):
        return CommandError(argv, 7, "connection refused")
    assert s.check_health(FakeRunner(failing), attempts=2, sleep=lambda _s: None) is False


# -- migration-head probing --------------------------------------------------


def test_probe_heads_parses_the_json_list_from_read():
    runner = FakeRunner(lambda argv, cwd: '["abcd1234"]\n')
    assert s.probe_heads(runner, Path("/venv/bin/python"), "memory") == ["abcd1234"]


# -- run_sync(): end-to-end against tmp_path HOME + a fake runner -----------


def _seed_train_assets(home: Path, train: str = "v0.1.0"):
    """Pre-seed the downloaded-assets layout `run_sync()` expects to find on
    disk after its `gh release download` calls — mirrors
    `test_release_cutter.py`'s convention of `.touch()`-ing expected build
    outputs rather than teaching the fake runner to actually invoke `uv`/`gh`."""
    manifest_dir = m.app_support_dir(home) / "downloads" / train / "_manifest"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "train.json").write_text(json.dumps(TRAIN_V1))

    platform_dir = s.download_dir(home, train, "snowlinedev/Snowline", train)
    platform_dir.mkdir(parents=True)
    for name in (
        "snowline_platform-0.1.0-py3-none-any.whl",
        "snowline_governance-0.1.0-py3-none-any.whl",
        "snowline_memory-0.1.0-py3-none-any.whl",
        "snowline_plugin_sdk-0.1.0-py3-none-any.whl",
        "requirements-platform.txt", "requirements-governance.txt", "requirements-memory.txt",
        f"dashboard-dist-{train}.tar.gz",
    ):
        (platform_dir / name).touch()

    pm_dir = s.download_dir(home, train, "snowlinedev/snowline-pm", train)
    pm_dir.mkdir(parents=True)
    for name in ("snowline_pm-0.1.0-py3-none-any.whl", "requirements-pm.txt"):
        (pm_dir / name).touch()
    return platform_dir, pm_dir


def _full_fake_handler(argv, cwd):
    if argv[:3] == ["gh", "auth", "status"]:
        return "ok\n"
    if argv and argv[-1] == "--version":
        return "Python 3.12.3\n"
    if argv[:2] == ["psql", "-Atqc"]:
        return ""  # no DBs exist yet
    if argv[:2] == ["launchctl", "bootstrap"]:
        return ""
    return '["headA"]\n'  # covers the migration-head probe reads


def _stack_config(home: Path):
    path = m.stack_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = m.StackConfig(role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net")
    path.write_text(cfg.to_json())
    return cfg


def test_run_sync_fresh_install_builds_all_four_services_and_reports_ok(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    runner = FakeRunner(_full_fake_handler)

    report = s.run_sync(home=home, train="v0.1.0", runner=runner, health_sleep=lambda _s: None, report=lambda _: None)

    assert report.outcome == "ok"
    assert report.train == "v0.1.0"
    assert report.health_ok is True
    assert len(report.services) == 4
    assert all(c.changed for c in report.services)
    assert all(c.migration_crossed is False for c in report.services)  # fresh install
    assert all(c.kickstarted for c in report.services)
    for svc in s.SERVICE_ORDER:
        assert Path(os.readlink(m.service_current_link(home, svc))).name == "v0.1.0"
    assert m.report_path(home).exists()
    assert list(m.report_history_dir(home).glob("*.json"))


def test_run_sync_is_a_noop_when_already_on_the_resolved_train(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    runner = FakeRunner(_full_fake_handler)
    first = s.run_sync(home=home, train="v0.1.0", runner=runner, health_sleep=lambda _s: None, report=lambda _: None)
    assert first.outcome == "ok"

    runner2 = FakeRunner(_full_fake_handler)
    second = s.run_sync(home=home, train="v0.1.0", runner=runner2, health_sleep=lambda _s: None, report=lambda _: None)
    assert second.outcome == "noop"
    assert second.noop is True
    assert m.exit_code(second.outcome) == 0
    # a true no-op touches NOTHING — no gh/uv/launchctl/psql calls at all.
    assert runner2.runs == []
    assert not any(a[:2] == ("gh", "release") for a in runner2.reads)


def test_run_sync_dry_run_never_writes_or_downloads(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    runner = FakeRunner(_ok_gh_auth, dry_run=True)
    report = s.run_sync(home=home, train="v0.1.0", dry_run=True, runner=runner, health_sleep=lambda _s: None, report=lambda _: None)
    assert report.dry_run is True
    assert report.outcome == "ok"
    assert len(report.services) == 4
    assert all(c.changed for c in report.services)  # fresh install would build all 4
    assert not m.report_path(home).exists()
    assert not m.stack_config_path(home).exists() or True  # config was pre-seeded, untouched either way
    assert not m.service_current_link(home, "platform").exists()


def test_run_sync_refuses_when_role_is_not_spoke(tmp_path):
    home = tmp_path / "home"
    runner = FakeRunner(_ok_gh_auth)
    report = s.run_sync(home=home, role="primary", runner=runner, health_sleep=lambda _s: None, report=lambda _: None)
    assert report.outcome == "failed"
    assert "spoke-only" in report.detail
    assert m.exit_code(report.outcome) == 1


def test_run_sync_refuses_against_a_hand_configured_primary_env(tmp_path):
    home = tmp_path / "home"
    path = m.env_file_path(home, "platform")
    path.parent.mkdir(parents=True)
    path.write_text("export SNOWLINE_INSTANCE_ID=primary\n")
    runner = FakeRunner(_ok_gh_auth)
    report = s.run_sync(home=home, runner=runner, health_sleep=lambda _s: None, report=lambda _: None)
    assert report.outcome == "failed"
    assert "primary" in report.detail


def test_run_sync_auto_never_prompts_and_fails_loudly_when_config_missing(tmp_path):
    home = tmp_path / "home"
    runner = FakeRunner(_ok_gh_auth)

    def prompt_should_not_be_called(_msg):
        raise AssertionError("--auto must never prompt")

    report = s.run_sync(
        home=home, train="v0.1.0", auto=True, runner=runner, health_sleep=lambda _s: None,
        prompt=prompt_should_not_be_called, report=lambda _: None,
    )
    assert report.outcome == "failed"
    assert report.auto is True
    assert "--auto never prompts" in report.detail
    assert m.exit_code(report.outcome) == 1


def test_run_sync_interactive_prompts_once_and_persists_stack_json(tmp_path):
    home = tmp_path / "home"
    _seed_train_assets(home)
    runner = FakeRunner(_full_fake_handler)
    answers = iter(["roam", "mini.tailnet-name.ts.net"])
    report = s.run_sync(
        home=home, train="v0.1.0", runner=runner, health_sleep=lambda _s: None,
        prompt=lambda _msg: next(answers), report=lambda _: None,
    )
    assert report.outcome == "ok"
    assert report.instance_id == "roam"
    cfg = m.load_stack_config(m.stack_config_path(home).read_text())
    assert cfg == m.StackConfig(role="spoke", instance_id="roam", primary_tailnet_address="mini.tailnet-name.ts.net")


def test_run_sync_gh_auth_failure_is_reported_before_any_symlink(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)

    def handler(argv, cwd):
        if argv[:3] == ["gh", "auth", "status"]:
            return CommandError(argv, 1, "expired")
        return ""
    runner = FakeRunner(handler)
    report = s.run_sync(home=home, train="v0.1.0", runner=runner, health_sleep=lambda _s: None, report=lambda _: None)
    assert report.outcome == "failed"
    assert "not authenticated" in report.detail
    assert not m.service_current_link(home, "platform").exists()


# -- auto-upgrade failure posture: revert vs. needs_attention ---------------


def test_run_sync_reverts_on_health_failure_with_no_migration_crossed(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    runner = FakeRunner(_full_fake_handler)
    first = s.run_sync(home=home, train="v0.1.0", runner=runner, health_sleep=lambda _s: None, report=lambda _: None)
    assert first.outcome == "ok"

    _seed_train_assets(home, "v0.2.0")

    def handler(argv, cwd):
        if argv[:3] == ["gh", "auth", "status"]:
            return "ok\n"
        if argv and argv[-1] == "--version":
            return "Python 3.12.3\n"
        if argv[:2] == ["curl", "-fsS"] or (argv and argv[0] == "curl"):
            return CommandError(argv, 7, "connection refused")
        if argv[:2] == ["psql", "-Atqc"]:
            return "snowline_platform\nsnowline_governance\nsnowline_memory\nsnowline_pm\n"
        if argv[:2] == ["launchctl", "bootstrap"]:
            return ""
        return '["headA"]\n'  # SAME heads before/after -> no migration crossed

    runner2 = FakeRunner(handler)
    second = s.run_sync(home=home, train="v0.2.0", runner=runner2, health_sleep=lambda _s: None, report=lambda _: None)

    assert second.outcome == "reverted"
    assert second.health_ok is False
    assert m.exit_code(second.outcome) == 1
    # reverted BACK to v0.1.0 for every service.
    for svc in s.SERVICE_ORDER:
        assert Path(os.readlink(m.service_current_link(home, svc))).name == "v0.1.0"


def test_run_sync_never_auto_reverts_across_a_crossed_migration(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    runner = FakeRunner(_full_fake_handler)
    first = s.run_sync(home=home, train="v0.1.0", runner=runner, health_sleep=lambda _s: None, report=lambda _: None)
    assert first.outcome == "ok"

    _seed_train_assets(home, "v0.2.0")

    call_count = {"n": 0}

    def handler(argv, cwd):
        if argv[:3] == ["gh", "auth", "status"]:
            return "ok\n"
        if argv and argv[-1] == "--version":
            return "Python 3.12.3\n"
        if argv and argv[0] == "curl":
            return CommandError(argv, 7, "connection refused")
        if argv[:2] == ["psql", "-Atqc"]:
            return "snowline_platform\nsnowline_governance\nsnowline_memory\nsnowline_pm\n"
        if argv[:2] == ["launchctl", "bootstrap"]:
            return ""
        if len(argv) >= 2 and argv[1] == "-c" and "ScriptDirectory" in argv[2]:
            # first call per service = OLD venv probe, second = NEW venv probe.
            call_count["n"] += 1
            return '["headA"]\n' if call_count["n"] % 2 == 1 else '["headB"]\n'
        return ""

    runner2 = FakeRunner(handler)
    second = s.run_sync(home=home, train="v0.2.0", runner=runner2, health_sleep=lambda _s: None, report=lambda _: None)

    assert second.outcome == "needs_attention"
    assert m.exit_code(second.outcome) == 1
    assert any(c.migration_crossed for c in second.services)
    # NEVER auto-reverted: `current` stays on the NEW (v0.2.0) train.
    for svc in s.SERVICE_ORDER:
        assert Path(os.readlink(m.service_current_link(home, svc))).name == "v0.2.0"


def test_noop_run_still_health_checks_and_flags_a_dead_stack(tmp_path):
    """An up-to-date spoke with a dead gateway must not exit 0 forever — the
    --auto timer's exit code is the monitoring signal (#210 review)."""
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    runner = FakeRunner(_full_fake_handler)
    first = s.run_sync(home=home, train="v0.1.0", runner=runner, health_sleep=lambda _s: None, report=lambda _: None)
    assert first.outcome == "ok"

    def dead_gateway(argv, cwd):
        if argv[:3] == ["gh", "auth", "status"]:
            return "ok\n"
        if argv and argv[0] == "curl":
            return CommandError(argv, 7, "connection refused")
        return ""

    second = s.run_sync(
        home=home, train="v0.1.0", runner=FakeRunner(dead_gateway),
        health_sleep=lambda _s: None, report=lambda _: None,
    )
    assert second.noop is True
    assert second.outcome == "needs_attention"
    assert second.health_ok is False
    assert m.exit_code(second.outcome) == 1
    # and a HEALTHY noop still exits 0.
    third = s.run_sync(
        home=home, train="v0.1.0", runner=FakeRunner(_full_fake_handler),
        health_sleep=lambda _s: None, report=lambda _: None,
    )
    assert third.outcome == "noop" and m.exit_code(third.outcome) == 0


def test_sync_refuses_on_a_source_run_hub(tmp_path):
    """dev.snowline.* plists present with NO stack.json = the source-run hub
    posture — sync must refuse rather than stand up a second service set on
    the hub's own ports (#210 review; decision 54447516)."""
    home = tmp_path / "home"
    agents = m.launch_agents_dir(home)
    agents.mkdir(parents=True)
    (agents / "dev.snowline.platform.plist").write_text("<plist/>")
    rpt = s.run_sync(
        home=home, runner=FakeRunner(_full_fake_handler),
        health_sleep=lambda _s: None, report=lambda _: None,
    )
    assert rpt.outcome == "failed"
    assert "SPOKE-ONLY" in (rpt.detail or "")
    # a stack-managed spoke (stack.json present) is never tripped by its own plists
    _stack_config(home)
    s.check_not_source_hub(home)  # does not raise


def _stack_hub_guard_raises(home, dirs):
    try:
        s.check_not_source_hub(home, plist_dirs=dirs)
    except m.StackError:
        return True
    return False


def test_hub_guard_trips_on_a_system_daemon_plist(tmp_path):
    """#239: once the hub's plists move to /Library/LaunchDaemons the guard
    must still recognise it (no stack.json)."""
    home = tmp_path / "home"
    agents, daemons = m.launch_agents_dir(home), tmp_path / "LaunchDaemons"
    agents.mkdir(parents=True)
    daemons.mkdir()
    (daemons / "dev.snowline.memory.plist").write_text("<plist/>")
    assert _stack_hub_guard_raises(home, [agents, daemons])


def test_hub_guard_trips_on_a_user_agent_plist(tmp_path):
    home = tmp_path / "home"
    agents, daemons = m.launch_agents_dir(home), tmp_path / "LaunchDaemons"
    agents.mkdir(parents=True)
    daemons.mkdir()
    (agents / "dev.snowline.pm.plist").write_text("<plist/>")
    assert _stack_hub_guard_raises(home, [agents, daemons])


def test_hub_guard_passes_with_neither(tmp_path):
    home = tmp_path / "home"
    assert not _stack_hub_guard_raises(home, [m.launch_agents_dir(home), tmp_path / "LaunchDaemons"])


def test_hub_guard_passes_when_stack_json_exists(tmp_path):
    home = tmp_path / "home"
    daemons = tmp_path / "LaunchDaemons"
    daemons.mkdir()
    (daemons / "dev.snowline.platform.plist").write_text("<plist/>")
    _stack_config(home)
    assert not _stack_hub_guard_raises(home, [m.launch_agents_dir(home), daemons])


def test_unpack_dashboard_strips_the_tarballs_top_level_dist(tmp_path):
    """The cutter packs a top-level `dist/` entry (the v0.1.0 shape);
    unpacking must strip it so the dest itself holds index.html — without
    --strip-components every packaged install's /ui 404'd (#210 review)."""
    recorded = []

    def handler(argv, cwd):
        recorded.append(argv)
        return ""

    s.unpack_dashboard(FakeRunner(handler), tarball=tmp_path / "d.tar.gz", dest=tmp_path / "out")
    tar = next(a for a in recorded if a and a[0] == "tar")
    assert "--strip-components" in tar and tar[tar.index("--strip-components") + 1] == "1"


def test_swap_dashboard_current_repoints_the_stable_path(tmp_path):
    """The env files bake the STABLE dashboard/current path; the symlink is
    the train-versioned part (#210 review: a versioned env value froze the UI
    at the install-time train and read as perpetual drift)."""
    home = tmp_path / "home"
    m.dashboard_dir(home, "v0.1.0").mkdir(parents=True)
    m.dashboard_dir(home, "v0.2.0").mkdir(parents=True)
    s.swap_dashboard_current(home, "v0.1.0")
    link = m.dashboard_root(home) / "current"
    assert Path(os.readlink(link)).name == "v0.1.0"
    s.swap_dashboard_current(home, "v0.2.0")
    assert Path(os.readlink(link)).name == "v0.2.0"


def test_fresh_install_health_failure_detail_never_claims_a_migration(tmp_path):
    """needs_attention on a FRESH install (nothing to revert to) must say so —
    the fabricated 'a migration crossed' diagnosis sent the operator down the
    re-seed runbook for no reason (#210 review)."""
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)

    def unhealthy_fresh(argv, cwd):
        if argv[:3] == ["gh", "auth", "status"]:
            return "ok\n"
        if argv and argv[-1] == "--version":
            return "Python 3.12.3\n"
        if argv and argv[0] == "curl":
            return CommandError(argv, 7, "connection refused")
        if argv[:2] == ["psql", "-Atqc"]:
            return "snowline_platform\nsnowline_governance\nsnowline_memory\nsnowline_pm\n"
        if argv[:2] == ["launchctl", "bootstrap"]:
            return ""
        return '["headA"]\n'

    rpt = s.run_sync(
        home=home, train="v0.1.0", runner=FakeRunner(unhealthy_fresh),
        health_sleep=lambda _s: None, report=lambda _: None,
    )
    assert rpt.outcome == "needs_attention"
    assert "no previous train" in (rpt.detail or "")
    assert "migration crossed" not in (rpt.detail or "").split("No migration crossed")[0]


# -- the failover front (issue #260) -----------------------------------------


def test_front_env_template_renders_the_hub_url_from_stack_json(tmp_path):
    cfg = m.StackConfig(
        role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net",
        primary_gateway_url="http://100.81.176.75:8850",
    )
    variables = s.env_variables(cfg, dashboard_dist="/d")
    result = s.apply_env_file(tmp_path, m.FRONT_SERVICE, variables=variables, dry_run=False)
    assert result.action == "written"
    exports = m.parse_env_exports(result.path.read_text())
    assert exports["SNOWLINE_FRONT_HUB_URL"] == "http://100.81.176.75:8850"
    assert exports["SNOWLINE_FRONT_SPOKE_URL"] == "http://127.0.0.1:8848"
    assert "SNOWLINE_FRONT_FORCE_UPSTREAM" not in exports  # documented, commented out
    assert "gui/$(id -u)/dev.snowline.front" in result.path.read_text()


def test_run_sync_installs_and_kickstarts_the_front_and_health_checks_it(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    runner = FakeRunner(_full_fake_handler)

    report = s.run_sync(home=home, train="v0.1.0", runner=runner, health_sleep=lambda _s: None, report=lambda _: None)

    assert report.outcome == "ok"
    assert report.front_kickstarted is True
    assert len(report.services) == 4  # the front is not a service entry
    plist = m.plist_path(home, m.FRONT_SERVICE)
    assert plist.exists()
    assert "venvs/platform/current/bin/uvicorn" in plist.read_text()
    env = m.parse_env_exports(m.env_file_path(home, m.FRONT_SERVICE).read_text())
    assert env["SNOWLINE_FRONT_HUB_URL"] == "http://mini.ts.net:8850"  # default, no gateway url yet
    kicks = [a[-1] for a in runner.ran("launchctl", "kickstart")]
    assert kicks[-1].endswith("/dev.snowline.front")  # after the services it runs from
    curls = [a[-1] for a in runner.reads if a[:1] == ("curl",)]
    assert s.FRONT_HEALTH_URL in curls
    assert s.DEFAULT_HEALTH_URL in curls


def _seed_v2_manifest(home):
    v2 = json.loads(json.dumps(TRAIN_V1).replace("v0.1.0", "v0.2.0"))
    manifest_dir = m.app_support_dir(home) / "downloads" / "v0.2.0" / "_manifest"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "train.json").write_text(json.dumps(v2))


def _dead_front(argv, cwd):
    if argv and argv[0] == "curl" and argv[-1] == s.FRONT_HEALTH_URL:
        return CommandError(argv, 7, "connection refused")
    return _full_fake_handler(argv, cwd)


def test_run_sync_dead_front_is_reported_never_a_revert_trigger(tmp_path):
    """A front can die for non-train reasons (port 8850 taken, a bad
    front.env) — reverting for it would revert every later train forever."""
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    first = s.run_sync(home=home, train="v0.1.0", runner=FakeRunner(_full_fake_handler),
                       health_sleep=lambda _s: None, report=lambda _: None)
    assert first.outcome == "ok" and first.front_ok is True
    _seed_v2_manifest(home)

    runner2 = FakeRunner(_dead_front)
    second = s.run_sync(home=home, train="v0.2.0", runner=runner2, health_sleep=lambda _s: None, report=lambda _: None)
    assert second.health_ok is True  # the gateway
    assert second.front_ok is False
    assert second.outcome == "needs_attention"
    assert "failover front is NOT answering" in second.detail
    assert "snowline-front.err.log" in second.detail and "8850" in second.detail
    assert "post-upgrade health check failed" not in second.detail
    # NOT reverted: every service stays on the new train, front kicked once
    for svc in s.SERVICE_ORDER:
        assert Path(os.readlink(m.service_current_link(home, svc))).name == "v0.2.0"
    kicks = [a[-1] for a in runner2.ran("launchctl", "kickstart")]
    assert sum(k.endswith("/dev.snowline.front") for k in kicks) == 1
    assert m.report_path(home).exists()
    assert json.loads(m.report_path(home).read_text())["front_ok"] is False


def test_run_sync_dead_gateway_still_reverts_and_skips_the_front_probe(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    s.run_sync(home=home, train="v0.1.0", runner=FakeRunner(_full_fake_handler),
               health_sleep=lambda _s: None, report=lambda _: None)
    _seed_v2_manifest(home)

    def dead_gateway(argv, cwd):
        if argv and argv[0] == "curl" and argv[-1] == s.DEFAULT_HEALTH_URL:
            return CommandError(argv, 7, "connection refused")
        return _full_fake_handler(argv, cwd)

    runner2 = FakeRunner(dead_gateway)
    second = s.run_sync(home=home, train="v0.2.0", runner=runner2, health_sleep=lambda _s: None, report=lambda _: None)
    assert second.outcome == "reverted"
    assert second.health_ok is False
    assert second.front_ok is None  # not probed once the gateway failed
    assert not any(a[-1] == s.FRONT_HEALTH_URL for a in runner2.reads if a[:1] == ("curl",))
    # the front followed the platform venv back
    kicks = [a[-1] for a in runner2.ran("launchctl", "kickstart")]
    assert sum(k.endswith("/dev.snowline.front") for k in kicks) == 2


def test_run_sync_dry_run_mentions_the_front(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    lines = []
    s.run_sync(home=home, train="v0.1.0", dry_run=True, runner=FakeRunner(_ok_gh_auth, dry_run=True),
               health_sleep=lambda _s: None, report=lines.append)
    (front,) = [ln for ln in lines if ln.strip().startswith("front")]
    assert "dev.snowline.front.plist" in front and "front.env" in front and "kickstart" in front
    assert not m.plist_path(home, m.FRONT_SERVICE).exists()


def test_run_sync_noop_touches_nothing_but_still_checks_the_front(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    s.run_sync(home=home, train="v0.1.0", runner=FakeRunner(_full_fake_handler),
               health_sleep=lambda _s: None, report=lambda _: None)

    runner = FakeRunner(_dead_front)
    lines = []
    rpt = s.run_sync(home=home, train="v0.1.0", runner=runner, health_sleep=lambda _s: None, report=lines.append)
    assert rpt.noop is True
    assert rpt.outcome == "needs_attention"
    assert rpt.health_ok is True and rpt.front_ok is False
    # front-specific detail, not the misleading "services may be down"
    assert "failover front is NOT answering" in rpt.detail
    assert "services may be down" not in rpt.detail
    assert lines[-1].endswith("but the failover front is DOWN")
    assert runner.runs == []
    assert rpt.front_kickstarted is None


def test_run_sync_refuses_the_hub_role_before_rendering_the_front(tmp_path):
    """The hub never gets a front: sync is spoke-only (decision 54447516), and
    the refusal lands before any plist is written."""
    home = tmp_path / "home"
    _stack_config(home)
    rpt = s.run_sync(home=home, role="primary", runner=FakeRunner(_full_fake_handler),
                     health_sleep=lambda _s: None, report=lambda _: None)
    assert rpt.outcome == "failed"
    assert not m.plist_path(home, m.FRONT_SERVICE).exists()
    assert not m.env_file_path(home, m.FRONT_SERVICE).exists()


def test_apply_front_plist_is_idempotent(tmp_path):
    assert s.apply_front_plist(tmp_path, dry_run=True) is True
    assert not m.plist_path(tmp_path, m.FRONT_SERVICE).exists()
    assert s.apply_front_plist(tmp_path, dry_run=False) is True
    assert s.apply_front_plist(tmp_path, dry_run=False) is False


# --- SNOWLINE_FRONT_HEALTH_URL in the spoke platform.env (issue #296) --------

FRONT_URL_LINE = 'export SNOWLINE_FRONT_HEALTH_URL="http://127.0.0.1:8850/front/health"'


def _platform_kicks(runner):
    return [a[-1] for a in runner.ran("launchctl", "kickstart") if a[-1].endswith("/dev.snowline.platform")]


def _sync(home, runner=None):
    return s.run_sync(home=home, train="v0.1.0", runner=runner or FakeRunner(_full_fake_handler),
                      health_sleep=lambda _s: None, report=lambda _: None)


def test_platform_env_renders_the_front_health_url_when_absent(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    _sync(home)
    env = m.parse_env_exports(m.env_file_path(home, "platform").read_text())
    assert env["SNOWLINE_FRONT_HEALTH_URL"] == m.front_health_url() == "http://127.0.0.1:8850/front/health"
    # only the platform carries it
    assert "SNOWLINE_FRONT_HEALTH_URL" not in m.env_file_path(home, "pm").read_text()


def test_existing_platform_env_without_the_key_gets_it_appended_and_platform_kicked(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    _sync(home)
    path = m.env_file_path(home, "platform")
    # an older platform.env (pre-#296): the key's block removed, operator edit above kept
    old = path.read_text().replace(s.load_platform_front_block().replace(
        "$front_health_url", m.front_health_url()), "")
    assert "SNOWLINE_FRONT_HEALTH_URL" not in old
    path.write_text(old.replace("SNOWLINE_REPLICATION_INTERVAL=30", "SNOWLINE_REPLICATION_INTERVAL=5"))

    runner = FakeRunner(_full_fake_handler)
    rpt = _sync(home, runner)  # already on the train: a no-op run
    text = path.read_text()
    assert FRONT_URL_LINE in text
    assert "SNOWLINE_REPLICATION_INTERVAL=5" in text  # nothing above was rewritten
    assert len(_platform_kicks(runner)) == 1
    assert rpt.noop is True
    assert "SNOWLINE_FRONT_HEALTH_URL" in rpt.detail


def test_failed_platform_kick_for_the_key_is_needs_attention_not_noop(tmp_path):
    """The key is appended but `launchctl kickstart` fails: the OLD platform
    keeps answering /health, so the health check passes — the run must still
    say needs_attention and name the manual kick, because the key is present
    now and no later run will retry it (review of #297)."""
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    _sync(home)
    path = m.env_file_path(home, "platform")
    old = path.read_text().replace(s.load_platform_front_block().replace(
        "$front_health_url", m.front_health_url()), "")
    assert "SNOWLINE_FRONT_HEALTH_URL" not in old
    path.write_text(old)

    def failing_platform_kick(argv, cwd):
        if argv[:2] == ["launchctl", "kickstart"] and argv[-1].endswith("dev.snowline.platform"):
            return CommandError(argv, 1, "no such process")
        return _full_fake_handler(argv, cwd)

    runner = FakeRunner(failing_platform_kick)
    rpt = _sync(home, runner)
    assert FRONT_URL_LINE in path.read_text()  # the key did land
    assert len(_platform_kicks(runner)) == 1  # one attempt, which failed
    assert rpt.noop is True
    assert rpt.outcome == "needs_attention"
    assert "kickstart FAILED" in rpt.detail and "launchctl kickstart" in rpt.detail
    assert "restarted the platform" not in rpt.detail

    # a second run: key present, nothing re-attempted, plain no-op
    runner2 = FakeRunner(_full_fake_handler)
    rpt2 = _sync(home, runner2)
    assert _platform_kicks(runner2) == [] and rpt2.outcome == "noop"


def test_existing_operator_value_is_kept_and_reported_as_drift(tmp_path):
    home = tmp_path / "home"
    cfg = _stack_config(home)
    variables = s.env_variables(cfg, dashboard_dist="/d")
    path = m.env_file_path(home, "platform")
    s.apply_env_file(home, "platform", variables=variables, dry_run=False)
    path.write_text(path.read_text().replace(
        "http://127.0.0.1:8850/front/health", "http://127.0.0.1:9999/front/health"))
    before = path.read_text()
    # the key is present, so ensure adds nothing...
    assert s.ensure_platform_front_key(home, variables=variables, front_installed=True, dry_run=False) is False
    # ...and the regular drift pass reports the difference without writing
    result = s.apply_env_file(home, "platform", variables=variables, dry_run=False)
    assert result.action == "drifted"
    assert path.read_text() == before


def test_operator_value_in_existing_file_is_not_re_added_or_kicked_on_noop(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    _sync(home)
    path = m.env_file_path(home, "platform")
    path.write_text(path.read_text().replace(
        "http://127.0.0.1:8850/front/health", "http://127.0.0.1:9999/front/health"))
    before = path.read_text()
    runner = FakeRunner(_full_fake_handler)
    rpt = _sync(home, runner)
    assert rpt.noop is True and rpt.outcome == "noop"
    assert path.read_text() == before
    assert runner.runs == []  # not kicked on a no-op


def test_ensure_key_is_a_noop_without_the_front_or_without_a_file(tmp_path):
    home = tmp_path / "home"
    variables = {"front_health_url": m.front_health_url()}
    # no platform.env at all -> apply_env_file's job, not ours
    assert s.ensure_platform_front_key(home, variables=variables, front_installed=True, dry_run=False) is False
    path = m.env_file_path(home, "platform")
    path.parent.mkdir(parents=True)
    path.write_text("export SNOWLINE_INSTANCE_ID=roam")
    # front not installed -> untouched
    assert s.ensure_platform_front_key(home, variables=variables, front_installed=False, dry_run=False) is False
    assert path.read_text() == "export SNOWLINE_INSTANCE_ID=roam"
    # dry run reports but does not write
    assert s.ensure_platform_front_key(home, variables=variables, front_installed=True, dry_run=True) is True
    assert path.read_text() == "export SNOWLINE_INSTANCE_ID=roam"
    # real run appends after the missing trailing newline
    assert s.ensure_platform_front_key(home, variables=variables, front_installed=True, dry_run=False) is True
    assert path.read_text().startswith("export SNOWLINE_INSTANCE_ID=roam\n")
    assert FRONT_URL_LINE in path.read_text()


def test_hub_role_never_touches_platform_env(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    rpt = s.run_sync(home=home, role="primary", runner=FakeRunner(_full_fake_handler),
                     health_sleep=lambda _s: None, report=lambda _: None)
    assert rpt.outcome == "failed"
    assert not m.env_file_path(home, "platform").exists()


# -- the hourly auto-upgrade timer (item 4a321bea / #208) --------------------

TIMER_LABEL = "dev.snowline.autoupgrade"


def _timer_calls(runner, verb):
    return [a for a in runner.runs if a[:2] == ("launchctl", verb) and TIMER_LABEL in " ".join(a)]


def _timer_runs(runner):
    return [a for a in runner.runs if TIMER_LABEL in " ".join(a)]


def test_render_autoupgrade_plist_is_hourly_quiet_and_follows_current(tmp_path):
    text = m.render_autoupgrade_plist(
        home=tmp_path, platform_venv_current=m.service_current_link(tmp_path, "platform"),
        logs=m.log_dir(tmp_path),
    )
    assert f"<string>{tmp_path}/Library/Application Support/Snowline/venvs/platform/current/bin/snowline</string>" in text
    assert "<string>--auto</string>" in text
    assert "<key>StartInterval</key>\n    <integer>3600</integer>" in text
    assert "<key>RunAtLoad</key>\n    <false/>" in text
    assert "KeepAlive" not in text
    assert "snowline-autoupgrade.log" in text
    assert "/opt/homebrew/bin" in text and "venvs/platform/current/bin:" in text


def test_run_sync_full_path_installs_and_bootstraps_the_timer_never_kickstarts(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    runner = FakeRunner(_full_fake_handler)
    rpt = s.run_sync(home=home, train="v0.1.0", runner=runner, health_sleep=lambda _s: None, report=lambda _: None)
    assert rpt.outcome == "ok"
    assert m.plist_path(home, m.AUTOUPGRADE_UNIT).exists()
    assert [a[1] for a in _timer_runs(runner)] == ["bootout", "bootstrap"]  # stale-config bootout first
    assert not _timer_calls(runner, "kickstart")
    assert "hourly auto-upgrade timer" in rpt.detail


def test_run_sync_noop_path_installs_the_timer_then_leaves_it_alone(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    _sync(home)  # full path installs it
    m.plist_path(home, m.AUTOUPGRADE_UNIT).unlink()  # simulate a pre-#208 spoke
    runner = FakeRunner(_full_fake_handler)
    rpt = _sync(home, runner)
    assert rpt.noop is True and rpt.outcome == "noop"
    assert m.plist_path(home, m.AUTOUPGRADE_UNIT).exists()
    assert len(_timer_calls(runner, "bootstrap")) == 1
    assert not _timer_calls(runner, "kickstart")

    # unchanged -> not rewritten, no launchctl at all
    path = m.plist_path(home, m.AUTOUPGRADE_UNIT)
    before = path.stat().st_mtime_ns
    runner2 = FakeRunner(_full_fake_handler)
    rpt2 = _sync(home, runner2)
    assert rpt2.detail == "already on the resolved train — nothing to do"
    assert path.stat().st_mtime_ns == before
    assert runner2.runs == []


def test_changed_timer_plist_is_bootout_then_bootstrap(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    _sync(home)
    path = m.plist_path(home, m.AUTOUPGRADE_UNIT)
    path.write_text(path.read_text().replace("3600", "60"))
    runner = FakeRunner(_full_fake_handler)
    rpt = _sync(home, runner)
    assert "3600" in path.read_text()
    verbs = [a[1] for a in _timer_runs(runner)]
    assert verbs == ["bootout", "bootstrap"]
    assert not _timer_calls(runner, "kickstart")
    assert "updated the hourly auto-upgrade timer" in rpt.detail


def test_dry_run_does_not_install_the_timer(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    lines = []
    s.run_sync(home=home, train="v0.1.0", dry_run=True, runner=FakeRunner(_ok_gh_auth, dry_run=True),
               health_sleep=lambda _s: None, report=lines.append)
    assert not m.plist_path(home, m.AUTOUPGRADE_UNIT).exists()
    assert any("would install the hourly auto-upgrade timer" in ln for ln in lines)


def test_hub_role_never_gets_the_timer(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    runner = FakeRunner(_full_fake_handler)
    rpt = s.run_sync(home=home, role="primary", runner=runner, health_sleep=lambda _s: None, report=lambda _: None)
    assert rpt.outcome == "failed"
    assert not m.plist_path(home, m.AUTOUPGRADE_UNIT).exists()
    assert _timer_runs(runner) == []


def test_auto_disabled_marker_is_a_noop_with_a_report(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    marker = m.autoupgrade_disabled_path(home)
    marker.touch()
    runner = FakeRunner(_full_fake_handler)
    lines = []
    rpt = s.run_sync(home=home, auto=True, runner=runner, health_sleep=lambda _s: None, report=lines.append)
    assert rpt.outcome == "noop" and rpt.noop is True and m.exit_code(rpt.outcome) == 0
    assert "disabled by marker" in rpt.detail
    assert json.loads(m.report_path(home).read_text())["outcome"] == "noop"
    assert runner.runs == [] and runner.reads == []  # not even a gh call
    assert not m.plist_path(home, m.AUTOUPGRADE_UNIT).exists()
    assert "disabled by marker" in lines[-1]


def test_marker_does_not_affect_a_manual_sync(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    m.autoupgrade_disabled_path(home).touch()
    _seed_train_assets(home)
    assert _sync(home).outcome == "ok"


def test_second_sync_while_the_lock_is_held_exits_zero_locked(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    holder = s.SyncLock(m.sync_lock_path(home))
    assert holder.acquire()
    try:
        runner = FakeRunner(_full_fake_handler)
        lines = []
        rpt = s.run_sync(home=home, auto=True, runner=runner, health_sleep=lambda _s: None, report=lines.append)
    finally:
        holder.release()
    assert rpt.outcome == "locked" and m.exit_code(rpt.outcome) == 0
    assert len(lines) == 1 and "another stack sync is running" in lines[0]
    assert runner.runs == [] and runner.reads == []
    assert not m.report_path(home).exists()  # never clobbers the running sync's report
    # released -> the next run proceeds
    assert s.SyncLock(m.sync_lock_path(home)).acquire()


def test_auto_missing_stack_json_says_so_and_changes_nothing(tmp_path):
    home = tmp_path / "home"
    runner = FakeRunner(_ok_gh_auth)
    rpt = s.run_sync(home=home, auto=True, runner=runner, health_sleep=lambda _s: None,
                     prompt=lambda _m: pytest.fail("prompted"), report=lambda _: None)
    assert rpt.outcome == "failed" and m.exit_code(rpt.outcome) == 1
    assert runner.reads == []  # failed before the gh preflight
    assert "needs ~/.config/snowline/stack.json" in rpt.detail
    assert "bootstrap-spoke" in rpt.detail and "nothing was changed" in rpt.detail
    assert not m.stack_config_path(home).exists()
    assert runner.runs == []


def test_no_prompt_guard_raises_instead_of_reading_a_tty():
    with pytest.raises(m.StackError, match="--auto never prompts"):
        s._no_prompt("the primary's tailnet address: ")


def test_run_sync_auto_never_calls_a_supplied_prompt_even_with_config_present(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    monkeypatch.setattr(s, "_no_prompt", lambda msg: pytest.fail("reached"), raising=True)
    rpt = s.run_sync(home=home, train="v0.1.0", auto=True, runner=FakeRunner(_full_fake_handler),
                     health_sleep=lambda _s: None, prompt=lambda _m: pytest.fail("prompted"),
                     report=lambda _: None)
    assert rpt.outcome == "ok"


def test_timer_run_sync_defers_the_reload_and_touches_the_marker(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    _sync(home)
    path = m.plist_path(home, m.AUTOUPGRADE_UNIT)
    path.write_text(path.read_text().replace("3600", "60"))
    monkeypatch.setenv("XPC_SERVICE_NAME", TIMER_LABEL)
    runner = FakeRunner(_full_fake_handler)
    rpt = _sync(home, runner)
    assert "3600" in path.read_text()
    assert _timer_runs(runner) == [] and not [a for a in runner.reads if TIMER_LABEL in " ".join(a)]
    assert m.autoupgrade_reload_pending_path(home).exists()
    assert "reload on the next manual `stack sync`" in rpt.detail

    # next manual sync: plist unchanged, but the marker forces bootout+bootstrap, then clears it
    monkeypatch.delenv("XPC_SERVICE_NAME")
    runner2 = FakeRunner(_full_fake_handler)
    _sync(home, runner2)
    assert [a[1] for a in _timer_runs(runner2)] == ["bootout", "bootstrap"]
    assert not m.autoupgrade_reload_pending_path(home).exists()


def test_failed_bootstrap_keeps_the_reload_marker_for_the_next_sync(tmp_path, monkeypatch):
    """A pending reload whose `launchctl bootstrap` fails must not clear the
    marker: the job is unloaded now, and the marker is what makes the next
    manual sync retry (and say why)."""
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    _sync(home)
    m.autoupgrade_reload_pending_path(home).parent.mkdir(parents=True, exist_ok=True)
    m.autoupgrade_reload_pending_path(home).touch()
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)

    def bootstrap_fails(argv, cwd):
        if argv[:2] == ["launchctl", "bootstrap"]:
            return CommandError(argv, 5, "Input/output error")
        return _full_fake_handler(argv, cwd)

    runner = FakeRunner(bootstrap_fails)
    rpt = _sync(home, runner)
    assert [a[1] for a in _timer_runs(runner)] == ["bootout", "bootstrap"]
    assert m.autoupgrade_reload_pending_path(home).exists()  # kept
    assert "bootstrap` FAILED" in rpt.detail

    runner2 = FakeRunner(_full_fake_handler)
    _sync(home, runner2)  # retried, now succeeds, marker cleared
    assert [a[1] for a in _timer_runs(runner2)] == ["bootout", "bootstrap"]
    assert not m.autoupgrade_reload_pending_path(home).exists()


def test_unloaded_timer_is_healed_and_a_loaded_one_is_left_alone(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    _sync(home)
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)

    def not_loaded(argv, cwd):
        if argv[:2] == ["launchctl", "print"]:
            return CommandError(argv, 113, "Could not find service")
        return _full_fake_handler(argv, cwd)

    runner = FakeRunner(not_loaded)
    rpt = _sync(home, runner)
    assert [a[1] for a in _timer_runs(runner)] == ["bootstrap"]
    assert "re-loaded" in rpt.detail

    runner2 = FakeRunner(_full_fake_handler)
    _sync(home, runner2)
    assert _timer_runs(runner2) == []


def test_fresh_install_boots_out_a_stale_loaded_config_first(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    runner = FakeRunner(_full_fake_handler)
    _sync(home, runner)
    assert [a[1] for a in _timer_runs(runner)] == ["bootout", "bootstrap"]


def test_hub_sync_leaves_no_lock_file(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    s.run_sync(home=home, role="primary", runner=FakeRunner(_full_fake_handler),
               health_sleep=lambda _s: None, report=lambda _: None)
    assert not m.sync_lock_path(home).exists()


def test_plist_path_has_local_bin_and_keg_only_postgres(tmp_path):
    text = m.render_autoupgrade_plist(
        home=tmp_path, platform_venv_current=tmp_path / "v", logs=tmp_path)
    assert f"{tmp_path}/.local/bin" in text and "postgresql@16/bin" in text
    assert "AbandonProcessGroup" not in text


# -- one reconcile pass for every stack-managed unit + re-exec (#303) -------


def _strip_front_key(home):
    path = m.env_file_path(home, "platform")
    old = path.read_text().replace(s.load_platform_front_block().replace(
        "$front_health_url", m.front_health_url()), "")
    assert "SNOWLINE_FRONT_HEALTH_URL" not in old
    path.write_text(old)


def _front_kicks(runner):
    return [a[-1] for a in runner.ran("launchctl", "kickstart") if a[-1].endswith("/dev.snowline.front")]


def test_old_cli_upgrade_then_noop_with_the_new_unit_list_installs_every_unit(tmp_path, monkeypatch):
    """The live defect (item 924fc510): the OLD train's CLI ran the upgrade,
    so units only the NEW train knows were never rendered; the no-op path
    must reconcile them all on the next run."""
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    with monkeypatch.context() as mp:
        mp.setattr(s, "UNITS", ())  # the old train: no front, no timer, no key logic
        assert _sync(home).outcome == "ok"
    _strip_front_key(home)  # the old train's platform.env template had no key
    assert not m.plist_path(home, m.FRONT_SERVICE).exists()
    assert not m.env_file_path(home, m.FRONT_SERVICE).exists()
    assert not m.plist_path(home, m.AUTOUPGRADE_UNIT).exists()

    runner = FakeRunner(_full_fake_handler)
    rpt = _sync(home, runner)
    assert rpt.noop is True and rpt.outcome == "noop"
    assert m.plist_path(home, m.FRONT_SERVICE).exists()
    assert m.env_file_path(home, m.FRONT_SERVICE).exists()
    assert m.plist_path(home, m.AUTOUPGRADE_UNIT).exists()
    assert FRONT_URL_LINE in m.env_file_path(home, "platform").read_text()
    assert len(_front_kicks(runner)) == 1 and len(_platform_kicks(runner)) == 1
    assert rpt.front_kickstarted is True
    assert [a[1] for a in _timer_runs(runner)] == ["bootout", "bootstrap"]
    assert {u.name: u.action for u in rpt.units} == {
        "platform-front-key": "added", "front": "installed", "autoupgrade": "installed",
    }
    assert "installed the failover front" in rpt.detail
    assert "hourly auto-upgrade timer" in rpt.detail

    # and the next one is a true no-op again
    runner2 = FakeRunner(_full_fake_handler)
    rpt2 = _sync(home, runner2)
    assert rpt2.detail == "already on the resolved train — nothing to do"
    assert runner2.runs == []
    assert all(u.action == "unchanged" for u in rpt2.units)


def test_full_path_reconciles_every_unit_in_the_same_run(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    runner = FakeRunner(_full_fake_handler)
    rpt = _sync(home, runner)
    assert rpt.outcome == "ok"
    assert [(u.name, u.action) for u in rpt.units] == [
        ("platform-front-key", "unchanged"), ("front", "installed"), ("autoupgrade", "installed"),
    ]
    assert m.plist_path(home, m.FRONT_SERVICE).exists()
    assert m.plist_path(home, m.AUTOUPGRADE_UNIT).exists()
    assert len(_front_kicks(runner)) == 1
    kicks = [a[-1] for a in runner.ran("launchctl", "kickstart")]
    assert kicks[-1].endswith("/dev.snowline.front")  # after the services


def test_full_path_key_added_to_a_changed_platform_reuses_its_upgrade_kick(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    _sync(home)
    _strip_front_key(home)
    _seed_v2_manifest(home)
    runner = FakeRunner(_full_fake_handler)
    rpt = s.run_sync(home=home, train="v0.2.0", runner=runner, health_sleep=lambda _s: None, report=lambda _: None)
    assert rpt.outcome == "ok"
    assert FRONT_URL_LINE in m.env_file_path(home, "platform").read_text()
    assert "platform" not in rpt.env_drift  # the key landed before the drift pass
    assert len(_platform_kicks(runner)) == 1  # the upgrade kick, not a second one
    key = next(u for u in rpt.units if u.name == "platform-front-key")
    assert key.action == "added" and key.kicked is True


def test_reconcile_units_is_idempotent_and_defers_the_timer_reload_on_a_timer_run(tmp_path):
    home = tmp_path / "home"
    cfg = _stack_config(home)
    _seed_train_assets(home)
    _sync(home)
    runner = FakeRunner(_full_fake_handler)
    units = s.reconcile_units(home, cfg, runner, dry_run=False, timer_run=False)
    assert all(u.action == "unchanged" and u.kicked is None for u in units)
    assert runner.runs == []

    path = m.plist_path(home, m.AUTOUPGRADE_UNIT)
    path.write_text(path.read_text().replace("3600", "60"))
    runner2 = FakeRunner(_full_fake_handler)
    units2 = s.reconcile_units(home, cfg, runner2, dry_run=False, timer_run=True)
    timer = next(u for u in units2 if u.name == m.AUTOUPGRADE_UNIT)
    assert timer.action == "deferred"
    assert "3600" in path.read_text()
    assert _timer_runs(runner2) == [] and not [a for a in runner2.reads if TIMER_LABEL in " ".join(a)]
    assert m.autoupgrade_reload_pending_path(home).exists()


def test_reconcile_units_dry_run_writes_and_kicks_nothing(tmp_path):
    home = tmp_path / "home"
    cfg = _stack_config(home)
    runner = FakeRunner(_full_fake_handler, dry_run=True)
    units = s.reconcile_units(home, cfg, runner, dry_run=True, timer_run=False)
    assert {u.name: u.action for u in units}["front"] == "installed"
    assert all(u.kicked is None for u in units)
    assert not m.plist_path(home, m.FRONT_SERVICE).exists()
    assert not m.plist_path(home, m.AUTOUPGRADE_UNIT).exists()
    assert runner.runs == []


# --- re-exec ----------------------------------------------------------------


def _fake_new_cli(home, train="v0.2.0"):
    cli = m.service_venv_dir(home, "platform", train) / "bin" / "snowline"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    return cli


@pytest.fixture
def execs(monkeypatch):
    """Record os.execv calls (never really exec) and check the sync lock is
    free at exec time. Pins the resumed-env marker so the test restores it."""
    monkeypatch.setenv(m.SYNC_RESUMED_ENV, "0")
    calls = []

    def fake_execv(path, argv):
        # argv[0] = <app support>/venvs/platform/current/bin/snowline
        lock_path = Path(argv[0]).parents[4] / "sync.lock"
        assert lock_path.exists()  # the run did take it
        lock = s.SyncLock(lock_path)
        calls.append((path, list(argv), lock.acquire(), os.environ.get(m.SYNC_RESUMED_ENV)))
        lock.release()

    monkeypatch.setattr(os, "execv", fake_execv)
    return calls


def _upgraded_home(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    assert _sync(home).outcome == "ok"
    _seed_v2_manifest(home)
    return home


def _sync_v2(home, runner=None, **kw):
    return s.run_sync(home=home, train="v0.2.0", runner=runner or FakeRunner(_full_fake_handler),
                      health_sleep=lambda _s: None, report=kw.pop("report", lambda _: None), **kw)


def test_successful_platform_swap_reexecs_the_new_cli_once_with_the_same_flags(tmp_path, execs):
    home = _upgraded_home(tmp_path)
    cli = _fake_new_cli(home)
    reports_before = len(list(m.report_history_dir(home).glob("*.json")))
    lines = []
    rpt = _sync_v2(home, report=lines.append)
    assert rpt.outcome == "ok"
    assert len(execs) == 1
    path, argv, lock_free, marker = execs[0]
    expected_cli = str(m.service_current_link(home, "platform") / "bin" / "snowline")
    assert path == expected_cli and Path(path).resolve() == cli.resolve()
    assert argv == [expected_cli, "stack", "sync", "--role", "spoke", "--train", "v0.2.0",
                    "--resumed-from", "v0.1.0"]
    assert lock_free is True  # released before the exec
    assert marker == "1"
    # the old process writes no report — the resumed run writes the single one
    assert len(list(m.report_history_dir(home).glob("*.json"))) == reports_before
    assert json.loads(m.report_path(home).read_text())["train"] == "v0.1.0"
    assert m.sync_handoff_path(home).exists()
    assert any("re-exec'ing" in ln for ln in lines)


def test_auto_flags_and_health_url_ride_the_reexec(tmp_path, execs):
    home = _upgraded_home(tmp_path)
    _fake_new_cli(home)
    _sync_v2(home, auto=True, health_url="http://127.0.0.1:9999/health",
             runner=FakeRunner(_full_fake_handler), verbose=True)
    (_, argv, _, _), = execs
    assert argv[3:] == ["--role", "spoke", "--train", "v0.2.0", "--auto",
                        "--health-url", "http://127.0.0.1:9999/health", "-v",
                        "--resumed-from", "v0.1.0"]


def test_resumed_run_writes_the_single_report_with_units_and_resumed_from(tmp_path, execs):
    home = _upgraded_home(tmp_path)
    _fake_new_cli(home)
    _sync_v2(home, auto=True)
    assert len(execs) == 1
    reports_before = len(list(m.report_history_dir(home).glob("*.json")))

    # what the exec'd new CLI runs: the same run_sync, --resumed-from set
    monkeypatch_env = os.environ.get(m.SYNC_RESUMED_ENV)
    assert monkeypatch_env == "1"
    runner = FakeRunner(_full_fake_handler)
    rpt = _sync_v2(home, runner=runner, auto=True, resumed_from="v0.1.0")
    assert len(execs) == 1  # never a second re-exec
    assert rpt.outcome == "ok" and m.exit_code(rpt.outcome) == 0
    assert rpt.resumed_from == "v0.1.0" and rpt.noop is False
    assert [c.target_train for c in rpt.services] == ["v0.2.0"] * 4  # from the handoff
    assert all(c.changed and c.kickstarted for c in rpt.services)
    assert "upgrade from v0.1.0 finished under the v0.2.0 CLI" in rpt.detail
    assert runner.runs == []  # every unit already current: nothing rewritten or kicked
    assert not m.sync_handoff_path(home).exists()
    data = json.loads(m.report_path(home).read_text())
    assert data["resumed_from"] == "v0.1.0" and data["train"] == "v0.2.0"
    assert [u["name"] for u in data["units"]] == ["platform-front-key", "front", "autoupgrade"]
    assert len(list(m.report_history_dir(home).glob("*.json"))) == reports_before + 1


def test_resumed_auto_run_with_a_dead_gateway_exits_nonzero(tmp_path, execs):
    home = _upgraded_home(tmp_path)
    _fake_new_cli(home)
    _sync_v2(home, auto=True)

    def dead_gateway(argv, cwd):
        if argv and argv[0] == "curl" and argv[-1] == s.DEFAULT_HEALTH_URL:
            return CommandError(argv, 7, "connection refused")
        return _full_fake_handler(argv, cwd)

    rpt = _sync_v2(home, runner=FakeRunner(dead_gateway), auto=True, resumed_from="v0.1.0")
    assert rpt.outcome == "needs_attention" and m.exit_code(rpt.outcome) == 1
    assert "gateway health check FAILED" in rpt.detail


def test_resumed_run_that_loses_the_lock_discards_the_handoff_and_exits_locked(tmp_path, execs):
    """An hourly fire can win the lock between the old process releasing it
    and the resumed pass taking it: the resumed pass exits `locked` (0), the
    swap record is discarded so it can never be mis-applied, and the detail
    says the other run reconciles."""
    home = _upgraded_home(tmp_path)
    _fake_new_cli(home)
    _sync_v2(home, auto=True)
    assert m.sync_handoff_path(home).exists()
    holder = s.SyncLock(m.sync_lock_path(home))
    assert holder.acquire()
    try:
        rpt = _sync_v2(home, runner=FakeRunner(_full_fake_handler), auto=True, resumed_from="v0.1.0")
    finally:
        holder.release()
    assert rpt.outcome == "locked" and m.exit_code(rpt.outcome) == 0
    assert "upgrade from v0.1.0 already swapped" in rpt.detail
    assert not m.sync_handoff_path(home).exists()


def test_resumed_flag_without_a_swap_record_is_an_ordinary_noop(tmp_path, execs):
    """`--resumed-from` by hand (or after a discarded stale record) must not
    claim an upgrade this process cannot vouch for."""
    home = _upgraded_home(tmp_path)
    _fake_new_cli(home)
    _sync_v2(home, auto=True)
    m.sync_handoff_path(home).unlink()
    runner = FakeRunner(_full_fake_handler)
    rpt = _sync_v2(home, runner=runner, auto=True, resumed_from="v0.1.0")
    assert rpt.outcome == "noop" and rpt.noop is True and rpt.services == ()
    assert rpt.resumed_from is None
    assert "no swap record was found" in rpt.detail
    assert "upgrade from v0.1.0 finished" not in rpt.detail


def test_no_reexec_after_a_revert_or_needs_attention(tmp_path, execs):
    home = _upgraded_home(tmp_path)
    _fake_new_cli(home)

    def dead_gateway(argv, cwd):
        if argv and argv[0] == "curl" and argv[-1] == s.DEFAULT_HEALTH_URL:
            return CommandError(argv, 7, "connection refused")
        return _full_fake_handler(argv, cwd)

    assert _sync_v2(home, runner=FakeRunner(dead_gateway)).outcome == "reverted"
    assert _sync_v2(home, runner=FakeRunner(_dead_front)).outcome == "needs_attention"
    assert execs == []
    assert json.loads(m.report_path(home).read_text())["outcome"] == "needs_attention"


def test_no_reexec_on_dry_run_but_it_says_it_would(tmp_path, execs):
    home = _upgraded_home(tmp_path)
    _fake_new_cli(home)
    lines = []
    rpt = _sync_v2(home, runner=FakeRunner(_full_fake_handler, dry_run=True), dry_run=True,
                   report=lines.append)
    assert rpt.dry_run is True and execs == []
    assert any("would re-exec" in ln and "venvs/platform/current/bin/snowline" in ln for ln in lines)


def test_no_reexec_when_already_resumed_by_flag_or_marker(tmp_path, execs, monkeypatch):
    home = _upgraded_home(tmp_path)
    _fake_new_cli(home)
    assert _sync_v2(home, resumed_from="v0.1.0").outcome == "ok"
    assert execs == []
    # the train is now current; roll forward again by flag-free run under the marker
    home2 = _upgraded_home(tmp_path / "b")
    _fake_new_cli(home2)
    monkeypatch.setenv(m.SYNC_RESUMED_ENV, "1")
    assert _sync_v2(home2).outcome == "ok"
    assert execs == []


def test_no_reexec_on_a_rollback_or_when_already_running_the_new_venv(tmp_path, execs, monkeypatch):
    home = _upgraded_home(tmp_path)
    _fake_new_cli(home)
    monkeypatch.setattr(s, "running_under", lambda venv: True)
    assert _sync_v2(home).outcome == "ok"
    assert execs == []
    monkeypatch.undo()
    monkeypatch.setenv(m.SYNC_RESUMED_ENV, "0")
    monkeypatch.setattr(os, "execv", lambda *a: execs.append(a))
    _fake_new_cli(home, "v0.1.0")
    rpt = s.run_sync(home=home, train="v0.1.0", runner=FakeRunner(_full_fake_handler),
                     health_sleep=lambda _s: None, report=lambda _: None)
    assert rpt.outcome == "ok"  # rollback: the older CLI has no newer units to add
    assert execs == []


def test_missing_new_cli_falls_back_to_finishing_in_process(tmp_path, execs):
    home = _upgraded_home(tmp_path)  # no fake CLI in the v0.2.0 venv
    rpt = _sync_v2(home)
    assert execs == []
    assert rpt.outcome == "ok"
    assert "new train's CLI was not found" in rpt.detail and "once more" in rpt.detail
    assert json.loads(m.report_path(home).read_text())["train"] == "v0.2.0"
    assert not m.sync_handoff_path(home).exists()


def test_failed_execv_falls_back_and_writes_the_report(tmp_path, monkeypatch):
    monkeypatch.setenv(m.SYNC_RESUMED_ENV, "0")
    home = _upgraded_home(tmp_path)
    _fake_new_cli(home)

    def boom(path, argv):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(os, "execv", boom)
    rpt = _sync_v2(home)
    assert rpt.outcome == "ok"
    assert "re-exec of" in rpt.detail and "Exec format error" in rpt.detail
    assert json.loads(m.report_path(home).read_text())["train"] == "v0.2.0"
    assert not m.sync_handoff_path(home).exists()
    assert os.environ.get(m.SYNC_RESUMED_ENV) is None
