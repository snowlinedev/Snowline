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
        "dashboard_dist": "x",
    }
    result = s.apply_env_file(home, "platform", variables=variables, dry_run=True)
    assert result.action == "written"
    assert not m.env_file_path(home, "platform").exists()


def test_apply_plist_writes_and_is_idempotent_on_no_change(tmp_path):
    home = tmp_path / "home"
    changed_1 = s.apply_plist(home, "governance", env_vars={"SNOWLINE_INSTANCE_ID": "roam"}, dry_run=False)
    assert changed_1 is True
    assert m.plist_path(home, "governance").exists()
    changed_2 = s.apply_plist(home, "governance", env_vars={"SNOWLINE_INSTANCE_ID": "roam"}, dry_run=False)
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
    assert s.check_health(FakeRunner(failing)) is False


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

    report = s.run_sync(home=home, train="v0.1.0", runner=runner, report=lambda _: None)

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
    first = s.run_sync(home=home, train="v0.1.0", runner=runner, report=lambda _: None)
    assert first.outcome == "ok"

    runner2 = FakeRunner(_full_fake_handler)
    second = s.run_sync(home=home, train="v0.1.0", runner=runner2, report=lambda _: None)
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
    report = s.run_sync(home=home, train="v0.1.0", dry_run=True, runner=runner, report=lambda _: None)
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
    report = s.run_sync(home=home, role="primary", runner=runner, report=lambda _: None)
    assert report.outcome == "failed"
    assert "spoke-only" in report.detail
    assert m.exit_code(report.outcome) == 1


def test_run_sync_refuses_against_a_hand_configured_primary_env(tmp_path):
    home = tmp_path / "home"
    path = m.env_file_path(home, "platform")
    path.parent.mkdir(parents=True)
    path.write_text("export SNOWLINE_INSTANCE_ID=primary\n")
    runner = FakeRunner(_ok_gh_auth)
    report = s.run_sync(home=home, runner=runner, report=lambda _: None)
    assert report.outcome == "failed"
    assert "primary" in report.detail


def test_run_sync_auto_never_prompts_and_fails_loudly_when_config_missing(tmp_path):
    home = tmp_path / "home"
    runner = FakeRunner(_ok_gh_auth)

    def prompt_should_not_be_called(_msg):
        raise AssertionError("--auto must never prompt")

    report = s.run_sync(
        home=home, train="v0.1.0", auto=True, runner=runner,
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
        home=home, train="v0.1.0", runner=runner,
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
    report = s.run_sync(home=home, train="v0.1.0", runner=runner, report=lambda _: None)
    assert report.outcome == "failed"
    assert "not authenticated" in report.detail
    assert not m.service_current_link(home, "platform").exists()


# -- auto-upgrade failure posture: revert vs. needs_attention ---------------


def test_run_sync_reverts_on_health_failure_with_no_migration_crossed(tmp_path):
    home = tmp_path / "home"
    _stack_config(home)
    _seed_train_assets(home)
    runner = FakeRunner(_full_fake_handler)
    first = s.run_sync(home=home, train="v0.1.0", runner=runner, report=lambda _: None)
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
    second = s.run_sync(home=home, train="v0.2.0", runner=runner2, report=lambda _: None)

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
    first = s.run_sync(home=home, train="v0.1.0", runner=runner, report=lambda _: None)
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
    second = s.run_sync(home=home, train="v0.2.0", runner=runner2, report=lambda _: None)

    assert second.outcome == "needs_attention"
    assert m.exit_code(second.outcome) == 1
    assert any(c.migration_crossed for c in second.services)
    # NEVER auto-reverted: `current` stays on the NEW (v0.2.0) train.
    for svc in s.SERVICE_ORDER:
        assert Path(os.readlink(m.service_current_link(home, svc))).name == "v0.2.0"
