"""`snowline release autocut` (#258) with git/gh/http/cut faked.

Same seam as test_release_cutter.py: the release runner's read()/run() split
replaces every subprocess, an injected `http_get` replaces the hub's /health
and /plugins, an injected `fetch_status` replaces pm's milestone_status, and
an injected `cut_fn` stands in for the ~6-minute `cut()` — what is under test
is the DECISION (moved? gated? which version? commit the record?), not the
cutter, which has its own suite.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

import pytest

from snowline_platform.release import autocut as a
from snowline_platform.release import cutter as cut_mod
from snowline_platform.release import model as m
from snowline_platform.release import runner as runner_mod

CONFIG_PATH = Path(__file__).resolve().parents[1] / "release" / "components.json"
REC_PLATFORM = "a" * 40
REC_PM = "b" * 40
NEW_PLATFORM = "c" * 40
NEW_PM = "d" * 40
HUB = "http://hub.test:8850"


def manifest(version="v0.5.1", platform=REC_PLATFORM, pm=REC_PM) -> dict:
    def entry(sha, wheel, kind="service", repo="snowlinedev/Snowline"):
        return {"repo": repo, "tag": version, "sha": sha, "wheel": wheel, "kind": kind}

    return {
        "version": version,
        "components": {
            "platform": entry(platform, "p.whl"),
            "governance": entry(platform, "g.whl"),
            "memory": entry(platform, "m.whl"),
            "sdk": entry(platform, "s.whl", kind="library"),
            "pm": entry(pm, "pm.whl", repo="snowlinedev/snowline-pm"),
        },
        "gated": {"milestone": "snowlinedev/v0.5", "forced": False, "required_remaining": 0},
    }


class FakeRunner:
    def __init__(self, world: World, *, dry_run=False):
        self.world = world
        self.dry_run = dry_run
        self.reads: list[tuple[str, ...]] = []
        self.runs: list[tuple[tuple[str, ...], Path | None]] = []

    def read(self, argv, *, cwd=None, env=None, check=True):
        self.reads.append(tuple(argv))
        out = self.world.read(list(argv), Path(cwd) if cwd else None)
        if isinstance(out, Exception):
            if not check:
                return ""
            raise out
        return out

    def run(self, argv, *, cwd=None, env=None, check=True):
        if self.dry_run:
            return ""
        self.runs.append((tuple(argv), cwd))
        out = self.world.run(list(argv), Path(cwd) if cwd else None)
        if isinstance(out, Exception):
            if not check:
                return ""
            raise out
        return out

    def ran(self, *prefix):
        return [argv for argv, _ in self.runs if argv[: len(prefix)] == prefix]


class World:
    """The two checkouts as git would describe them."""

    def __init__(self, checkouts, *, heads=None, origins=None, branch="main",
                 dirty=False, record_dirty=False, committed_manifest=None,
                 diff_names=None, gh_ok=True, push_fails=False, tags=None):
        self.checkouts = checkouts
        self.heads = heads or {"platform": NEW_PLATFORM, "pm": REC_PM}
        self.origins = origins or dict(self.heads)
        self.branch = branch
        self.dirty = dirty
        self.record_dirty = record_dirty
        self.committed_manifest = committed_manifest
        self.diff_names = diff_names if diff_names is not None else {"platform": "src/x.py\n"}
        self.gh_ok = gh_ok
        self.push_fails = push_fails
        self.tags = tags or {}

    def comp(self, cwd: Path | None) -> str:
        for name, path in self.checkouts.items():
            if cwd == path:
                return name
        return ""

    def read(self, argv, cwd):
        comp = self.comp(cwd)
        if argv[:3] == ["gh", "auth", "status"]:
            return "ok\n" if self.gh_ok else runner_mod.CommandError(argv, 1, "not logged in")
        if argv[:2] == ["git", "rev-parse"]:
            if argv[2:3] == ["--is-inside-work-tree"]:
                return "true\n"
            if argv[2:3] == ["--abbrev-ref"]:
                return f"{self.branch}\n"
            if argv[2:] == ["--verify", "--quiet", "origin/main"]:
                return f"{self.origins[comp]}\n"
            if argv[2:] == ["HEAD"]:
                return f"{self.heads[comp]}\n"
            return ""
        if argv[:2] == ["git", "status"]:
            if "--" in argv:  # the record-only probe
                return " M release/train.json\n" if (comp == "platform" and self.record_dirty) else ""
            lines = []
            if self.dirty:
                lines.append(" M src/x.py")
            if comp == "platform" and self.record_dirty:
                lines.append(" M release/train.json")
            return "\n".join(lines) + ("\n" if lines else "")
        if argv[:2] == ["git", "merge-base"]:
            return ""
        if argv[:2] == ["git", "diff"]:
            return self.diff_names.get(comp, "")
        if argv[:2] == ["git", "show"]:
            if self.committed_manifest is None:
                return runner_mod.CommandError(argv, 128, "no such path")
            return json.dumps(self.committed_manifest)
        if argv[:2] == ["git", "ls-remote"]:
            want = argv[-1].removeprefix("refs/tags/")
            if want.endswith("^{}"):
                return ""
            sha = self.tags.get((comp, want))
            return f"{sha}\trefs/tags/{want}\n" if sha else ""
        return ""

    def run(self, argv, cwd):
        if argv[:2] == ["git", "push"] and self.push_fails:
            return runner_mod.CommandError(argv, 1, "rejected (non-fast-forward)")
        if argv[:2] == ["git", "commit"]:
            self.heads["platform"] = "e" * 40
        return ""


def plugins_body(statuses=None):
    statuses = statuses or {"platform": "up", "pm": "up", "governance": "up", "memory": "up"}
    return {"plugins": [{"name": n, "status": s} for n, s in statuses.items()]}


def http(health=200, statuses=None, raise_on=None):
    calls = []

    def get(url):
        calls.append(url)
        if raise_on and url.endswith(raise_on):
            raise ConnectionError("refused")
        if url.endswith("/health"):
            return health, {"status": "ok"}
        return 200, plugins_body(statuses)

    get.calls = calls
    return get


def milestone_ok(_milestone):
    return {"registry": {"id": "x"}, "completion": {"required_remaining": {"count": 0}}}


class CutSpy:
    def __init__(self, *, raises=None, writes_record=True):
        self.calls = []
        self.raises = raises
        self.writes_record = writes_record

    def __call__(self, config, version, *, checkouts, out_dir, runner, report, fetch_status, **kw):
        self.calls.append({"version": version, "dry_run": runner.dry_run, **kw})
        if self.raises:
            raise self.raises
        if self.writes_record and not runner.dry_run:
            path = checkouts["platform"] / cut_mod.MANIFEST_RELPATH
            path.write_text(json.dumps(manifest(version, NEW_PLATFORM)))
            runner.world.record_dirty = True


@pytest.fixture()
def config():
    return m.load_config(CONFIG_PATH)


@pytest.fixture()
def checkouts(tmp_path):
    paths = {"platform": tmp_path / "Snowline", "pm": tmp_path / "snowline-pm"}
    for p in paths.values():
        p.mkdir()
    (paths["platform"] / "release").mkdir()
    (paths["platform"] / "release" / "train.json").write_text(json.dumps(manifest()))
    return paths


@pytest.fixture()
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def run(config, checkouts, home, world, *, cut=None, http_get=None, fetch=milestone_ok,
        dry_run=False, platform_url=HUB, lines=None, **kw):
    runner = FakeRunner(world, dry_run=dry_run)
    lines = lines if lines is not None else []
    cut = cut or CutSpy()
    rpt = a.run_autocut(
        config, checkouts=checkouts, home=home, runner=runner,
        platform_url=platform_url, dry_run=dry_run, report=lines.append,
        http_get=http_get or http(), fetch_status=fetch, cut_fn=cut, **kw,
    )
    return rpt, runner, cut


def read_report(home):
    return json.loads(a.report_path(home).read_text())


# -- pure helpers ----------------------------------------------------------


def test_next_patch_bumps_only_the_patch():
    assert a.next_patch("v0.5.0") == "v0.5.1"
    assert a.next_patch("v0.5.9") == "v0.5.10"
    with pytest.raises(m.ReleaseError):
        a.next_patch("v1.0")


def test_expected_plugins_are_the_train_services_not_the_sdk(config):
    assert sorted(a.expected_plugins(config)) == ["governance", "memory", "platform", "pm"]


# -- nothing moved ---------------------------------------------------------


def test_nothing_moved_exits_zero_without_cutting(config, checkouts, home):
    world = World(checkouts, heads={"platform": REC_PLATFORM, "pm": REC_PM})
    get = http()
    rpt, runner, cut = run(config, checkouts, home, world, http_get=get)
    assert rpt.outcome == a.OUTCOME_NOOP
    assert a.exit_code(rpt.outcome) == 0
    assert cut.calls == []
    assert runner.runs == []
    assert get.calls == []  # quiet: no gates probed when nothing moved
    data = read_report(home)
    assert data["outcome"] == "noop"
    assert not a.report_history_dir(home).exists()


def test_only_our_own_record_commit_on_main_is_not_movement(config, checkouts, home):
    """The record commit autocut pushes moves origin/main past the recorded
    sha; it must not re-trigger a cut every hour forever."""
    world = World(
        checkouts, heads={"platform": NEW_PLATFORM, "pm": REC_PM},
        diff_names={"platform": "release/train.json\n"},
    )
    rpt, _runner, cut = run(config, checkouts, home, world)
    assert rpt.outcome == a.OUTCOME_NOOP
    assert cut.calls == []


# -- moved + gates ok ------------------------------------------------------


def test_moved_and_gates_ok_cuts_next_patch_and_commits_only_the_record(config, checkouts, home):
    world = World(checkouts)
    rpt, runner, cut = run(config, checkouts, home, world)
    assert rpt.outcome == a.OUTCOME_OK, rpt.detail
    assert a.exit_code(rpt.outcome) == 0
    assert [c["version"] for c in cut.calls] == ["v0.5.2"]
    platform = checkouts["platform"]
    assert runner.ran("git", "add") == [("git", "add", "--", "release/train.json")]
    (commit,) = runner.ran("git", "commit")
    assert commit[-2:] == ("--", "release/train.json")
    assert "--only" in commit
    assert "release: train v0.5.2 (autocut)" in commit
    assert runner.ran("git", "push") == [("git", "push", "origin", "HEAD:main")]
    assert all(cwd == platform for argv, cwd in runner.runs)
    data = read_report(home)
    assert data["outcome"] == "ok"
    assert data["version"] == "v0.5.2"
    assert data["previous_version"] == "v0.5.1"
    assert data["last_cut"]["version"] == "v0.5.2"
    assert data["last_cut"]["shas"]["platform"] == NEW_PLATFORM
    assert data["record_commit"] == "e" * 40
    assert list(a.report_history_dir(home).iterdir())


def test_last_cut_is_carried_forward_across_noop_runs(config, checkouts, home):
    run(config, checkouts, home, World(checkouts))
    # Next hour: the record now says v0.5.2 at NEW_PLATFORM and nothing moved.
    world = World(checkouts, heads={"platform": NEW_PLATFORM, "pm": REC_PM}, diff_names={})
    rpt, _r, _c = run(config, checkouts, home, world)
    assert rpt.outcome == a.OUTCOME_NOOP
    assert read_report(home)["last_cut"]["version"] == "v0.5.2"


@pytest.mark.parametrize(("strict", "outcome"), [(False, a.OUTCOME_OK), (True, a.OUTCOME_GATED)])
def test_degraded_plugin_passes_by_default_but_not_under_strict(config, checkouts, home, strict, outcome):
    """`degraded` = 2xx + a self-reported soft issue, still routed; on the hub
    it is usually pm's replication to the OFFLINE standby — the very state
    this timer exists to fix, so it must not stall the train by default."""
    statuses = {"platform": "up", "pm": "degraded", "governance": "up", "memory": "up"}
    rpt, _r, cut = run(
        config, checkouts, home, World(checkouts),
        http_get=http(statuses=statuses), strict_health=strict,
    )
    assert rpt.outcome == outcome
    assert len(cut.calls) == (0 if strict else 1)


def test_a_taken_tag_at_another_sha_is_skipped_not_redefined(config, checkouts, home):
    world = World(checkouts, tags={("platform", "v0.5.2"): "f" * 40})
    _rpt, _r, cut = run(config, checkouts, home, world)
    assert [c["version"] for c in cut.calls] == ["v0.5.3"]


def test_a_tag_left_at_the_same_sha_is_reused_as_recovery(config, checkouts, home):
    world = World(checkouts, tags={("platform", "v0.5.2"): NEW_PLATFORM})
    _rpt, _r, cut = run(config, checkouts, home, world)
    assert [c["version"] for c in cut.calls] == ["v0.5.2"]


# -- gate failures: exit 2, cut NOT called, report written -----------------


def _assert_gated(rpt, runner, cut, home, gate_name):
    assert rpt.outcome == a.OUTCOME_GATED
    assert a.exit_code(rpt.outcome) == 2
    assert cut.calls == []
    assert runner.runs == []
    data = read_report(home)
    assert data["outcome"] == "gated"
    assert any(g["name"] == gate_name and not g["ok"] for g in data["gates"])


@pytest.mark.parametrize(
    ("world_kw", "gate_name"),
    [
        ({"branch": "feature"}, "checkout:platform"),
        ({"dirty": True}, "checkout:platform"),
        ({"heads": {"platform": NEW_PLATFORM, "pm": REC_PM},
          "origins": {"platform": "9" * 40, "pm": REC_PM}}, "checkout:platform"),
        ({"gh_ok": False}, "gh_auth"),
    ],
    ids=["not-on-main", "dirty", "behind-origin-main", "gh-unauthenticated"],
)
def test_checkout_and_auth_gates_refuse(config, checkouts, home, world_kw, gate_name):
    rpt, runner, cut = run(config, checkouts, home, World(checkouts, **world_kw))
    _assert_gated(rpt, runner, cut, home, gate_name)


@pytest.mark.parametrize(
    "http_get",
    [
        http(health=503),
        http(raise_on="/health"),
        http(statuses={"platform": "up", "pm": "up", "governance": "up"}),  # memory missing
        http(statuses={"platform": "up", "pm": "down", "governance": "up", "memory": "up"}),
    ],
    ids=["health-503", "health-unreachable", "plugin-unregistered", "plugin-down"],
)
def test_health_gate_refuses(config, checkouts, home, http_get):
    rpt, runner, cut = run(config, checkouts, home, World(checkouts), http_get=http_get)
    _assert_gated(rpt, runner, cut, home, "hub_health")


def test_unset_platform_url_refuses(config, checkouts, home):
    rpt, runner, cut = run(config, checkouts, home, World(checkouts), platform_url=None)
    _assert_gated(rpt, runner, cut, home, "hub_health")


def test_open_milestone_refuses(config, checkouts, home):
    def blocked(_m):
        return {"registry": {"id": "x"}, "completion": {"required_remaining": {"count": 2, "items": []}}}

    rpt, runner, cut = run(config, checkouts, home, World(checkouts), fetch=blocked)
    _assert_gated(rpt, runner, cut, home, "milestone")


def test_milestone_gate_that_cannot_judge_refuses_unattended(config, checkouts, home):
    def unreachable(_m):
        raise ConnectionError("pm down")

    rpt, runner, cut = run(config, checkouts, home, World(checkouts), fetch=unreachable)
    _assert_gated(rpt, runner, cut, home, "milestone")


def test_hand_edited_record_refuses(config, checkouts, home):
    world = World(checkouts, record_dirty=True, committed_manifest=manifest("v0.5.1"))
    rpt, runner, cut = run(config, checkouts, home, world)
    _assert_gated(rpt, runner, cut, home, "train_record")


# -- lock ------------------------------------------------------------------


def test_lock_held_is_a_noop(config, checkouts, home):
    path = a.lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rpt, runner, cut = run(config, checkouts, home, World(checkouts))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert rpt.outcome == a.OUTCOME_LOCKED
    assert a.exit_code(rpt.outcome) == 0
    assert cut.calls == []
    assert runner.reads == [] and runner.runs == []
    assert not a.report_path(home).exists()


def test_lock_is_released_after_a_run(config, checkouts, home):
    run(config, checkouts, home, World(checkouts))
    rpt, _r, _cut = run(config, checkouts, home, World(checkouts, diff_names={}))
    assert rpt.outcome != a.OUTCOME_LOCKED


# -- cut failure -----------------------------------------------------------


def test_cut_failure_commits_nothing_and_reports_failed(config, checkouts, home):
    cut = CutSpy(raises=m.ReleaseError("smoke failed for pm"))
    rpt, runner, _ = run(config, checkouts, home, World(checkouts), cut=cut)
    assert rpt.outcome == a.OUTCOME_FAILED
    assert a.exit_code(rpt.outcome) == 1
    assert runner.ran("git", "commit") == []
    assert runner.ran("git", "push") == []
    data = read_report(home)
    assert data["outcome"] == "failed"
    assert "smoke failed" in data["detail"]
    assert data["last_cut"]["outcome"] == "failed"


def test_unexpected_exception_still_writes_a_report(config, checkouts, home):
    cut = CutSpy(raises=RuntimeError("boom"))
    rpt, _runner, _ = run(config, checkouts, home, World(checkouts), cut=cut)
    assert rpt.outcome == a.OUTCOME_FAILED
    assert "boom" in read_report(home)["detail"]


def test_rejected_record_push_undoes_the_commit_and_fails(config, checkouts, home):
    world = World(checkouts, push_fails=True)
    rpt, runner, cut = run(config, checkouts, home, world)
    assert [c["version"] for c in cut.calls] == ["v0.5.2"]
    assert rpt.outcome == a.OUTCOME_FAILED
    assert runner.ran("git", "reset") == [("git", "reset", "--soft", "HEAD~1")]
    assert "published" in rpt.detail


# -- recovering an uncommitted record --------------------------------------


def test_uncommitted_newer_record_is_committed_even_when_nothing_moved(config, checkouts, home):
    """What a manual `release cut` without --commit-manifest leaves behind
    (v0.4.0's record was lost this way): the record is landed, nothing cut."""
    (checkouts["platform"] / "release" / "train.json").write_text(
        json.dumps(manifest("v0.5.2", NEW_PLATFORM))
    )
    world = World(
        checkouts, heads={"platform": NEW_PLATFORM, "pm": REC_PM}, diff_names={},
        record_dirty=True, committed_manifest=manifest("v0.5.1"),
    )
    rpt, runner, cut = run(config, checkouts, home, world)
    assert rpt.outcome == a.OUTCOME_OK, rpt.detail
    assert cut.calls == []
    (commit,) = runner.ran("git", "commit")
    assert "release: train v0.5.2 (autocut: record recovered)" in commit
    assert runner.ran("git", "push")


# -- dry run ---------------------------------------------------------------


def test_dry_run_plans_without_mutating_or_reporting(config, checkouts, home):
    lines: list[str] = []
    rpt, runner, cut = run(config, checkouts, home, World(checkouts), dry_run=True, lines=lines)
    assert rpt.outcome == a.OUTCOME_DRY_RUN
    assert a.exit_code(rpt.outcome) == 0
    assert runner.runs == []
    assert [c["dry_run"] for c in cut.calls] == [True]
    assert any("would cut v0.5.2" in line for line in lines)
    assert not a.report_path(home).exists()


def test_no_commit_manifest_leaves_the_record_uncommitted(config, checkouts, home):
    rpt, runner, _cut = run(config, checkouts, home, World(checkouts), commit_manifest=False)
    assert rpt.outcome == a.OUTCOME_OK
    assert runner.ran("git", "commit") == []


# -- cut(commit_manifest=True) — the manual `release cut --commit-manifest` --


def test_commit_train_record_skips_when_already_committed(checkouts):
    world = World(checkouts)
    runner = FakeRunner(world)
    assert cut_mod.commit_train_record(runner, checkouts["platform"], "v0.5.2", report=lambda _l: None) is None
    assert runner.runs == []


def test_commit_train_record_refuses_off_main(checkouts):
    world = World(checkouts, branch="feature", record_dirty=True)
    runner = FakeRunner(world)
    with pytest.raises(cut_mod.ManifestCommitError, match="not main"):
        cut_mod.commit_train_record(runner, checkouts["platform"], "v0.5.2", report=lambda _l: None)
    assert runner.runs == []


@pytest.mark.parametrize("flag", [False, True])
def test_manual_cut_commits_the_record_only_with_commit_manifest(config, checkouts, tmp_path, monkeypatch, flag):
    """`release cut --commit-manifest`: the opt-in manual path to the same
    record commit, run only after `record_manifest` (i.e. after publish)."""
    order: list[str] = []
    for name in ("run_tests", "build", "write_manifest", "smoke", "publish"):
        monkeypatch.setattr(cut_mod.Cutter, name, lambda self, *a_, **k: None)
    monkeypatch.setattr(
        cut_mod.Cutter, "record_manifest", lambda self, plan: order.append("record")
    )
    monkeypatch.setattr(
        cut_mod, "commit_train_record",
        lambda runner, checkout, version, **kw: order.append(f"commit {version} {checkout.name}"),
    )
    world = World(checkouts, heads={"platform": NEW_PLATFORM, "pm": NEW_PM})
    cut_mod.cut(
        config, "v0.5.2", checkouts=checkouts, out_dir=tmp_path / "b",
        runner=FakeRunner(world), report=lambda _l: None,
        fetch_status=milestone_ok, commit_manifest=flag,
    )
    assert order == (["record", "commit v0.5.2 Snowline"] if flag else ["record"])
