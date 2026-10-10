"""The release orchestration (issue #202) with the git/gh/uv/npm seams faked.

`release/runner.py` is the ONLY place the cutter shells out, and it draws one
line — `read()` for non-mutating commands, `run()` for mutating ones — so a
fake with those two methods replaces the entire outside world. What is asserted
here is the behaviour that would otherwise only be observable by cutting a real
train: that a dry run mutates nothing, that a re-run after a partial failure
skips what already happened instead of duplicating it, that a respin pulls the
carried-forward component's wheels from its recorded tag, and that pm's lock
export leaves the build directory with no git dependency in it.

The end-to-end cut is deliberately NOT run here. Cutting v0.1.0 is an operator
action.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snowline_platform.release import cutter as cut_mod
from snowline_platform.release import model as m
from snowline_platform.release import runner as runner_mod

CONFIG_PATH = Path(__file__).resolve().parents[1] / "release" / "components.json"
PLATFORM_SHA = "a" * 40
PM_SHA = "b" * 40


class FakeRunner:
    """Stands in for `release.runner.Runner`, same two-method contract."""

    def __init__(self, handler, *, dry_run: bool = False):
        self.handler = handler
        self.dry_run = dry_run
        self.reads: list[tuple[tuple[str, ...], Path | None]] = []
        self.runs: list[tuple[tuple[str, ...], Path | None]] = []
        self.report_lines: list[str] = []

    def read(self, argv, *, cwd=None, env=None, check=True):
        self.reads.append((tuple(argv), cwd))
        out = self.handler(list(argv), cwd)
        if isinstance(out, Exception):
            if not check:
                return ""
            raise out
        return out

    def run(self, argv, *, cwd=None, env=None, check=True):
        if self.dry_run:
            return ""
        self.runs.append((tuple(argv), cwd))
        return ""

    # -- assertions helpers --
    def ran(self, *prefix: str) -> list[tuple[str, ...]]:
        return [a for a, _ in self.runs if a[: len(prefix)] == prefix]

    def did_read(self, *prefix: str) -> bool:
        return any(a[: len(prefix)] == prefix for a, _ in self.reads)


def make_handler(*, heads=None, dirty=False, branch="main", tags=None, releases=(), pushed=True):
    heads = heads or {}
    tags = tags or {}

    def handler(argv, cwd):
        name = Path(cwd).name if cwd else ""
        head = heads.get(name, PLATFORM_SHA)
        if argv[:2] == ["git", "rev-parse"]:
            if argv[2:3] == ["--is-inside-work-tree"]:
                return "true\n"
            if argv[2:3] == ["--abbrev-ref"]:
                return f"{branch}\n"
            if argv[2:] == ["--verify", "--quiet", "origin/main"]:
                return f"{head}\n" if pushed else f"{'f' * 40}\n"
            if argv[2:3] == ["--verify"]:
                # Local-tag lookups (`refs/tags/<v>^{commit}`): none exist
                # unless a test seeds one under a ("<dir>", "local:<tag>") key.
                ref = argv[4].removeprefix("refs/tags/").removesuffix("^{commit}")
                sha = tags.get((name, f"local:{ref}"))
                return f"{sha}\n" if sha else ""
            return f"{head}\n"
        if argv[:2] == ["git", "status"]:
            return " M src/x.py\n" if dirty else ""
        if argv[:2] == ["git", "merge-base"]:
            return "" if pushed else runner_mod.CommandError(argv, 1, "not an ancestor")
        if argv[:2] == ["git", "ls-remote"]:
            want = argv[-1].removeprefix("refs/tags/")
            if want.endswith("^{}"):
                return ""
            sha = tags.get((name, want))
            return f"{sha}\trefs/tags/{want}\n" if sha else ""
        if argv[:2] == ["git", "describe"]:
            return "v0.1.0\n"
        if argv[:2] == ["git", "log"]:
            return "Something landed (#42)\n"
        if argv[:3] == ["gh", "auth", "status"]:
            return "ok\n"
        if argv[:3] == ["gh", "release", "view"]:
            # `gh` is repo-scoped by flag, not by cwd.
            repo = argv[argv.index("--repo") + 1]
            if (repo, argv[3]) in releases:
                return json.dumps({"tagName": argv[3]})
            return runner_mod.CommandError(argv, 1, "release not found")
        return ""

    return handler


@pytest.fixture()
def config() -> m.ReleaseConfig:
    return m.load_config(CONFIG_PATH)


@pytest.fixture()
def checkouts(tmp_path) -> dict[str, Path]:
    paths = {"platform": tmp_path / "Snowline", "pm": tmp_path / "snowline-pm"}
    for p in paths.values():
        p.mkdir()
    return paths


def _cutter(config, checkouts, runner, tmp_path, **kw) -> cut_mod.Cutter:
    # Direct-Cutter tests get the sdk version pre-resolved, the way `cut()`
    # sets it from `m.sdk_train_version(config, plan)`.
    kw.setdefault("sdk_version", "0.1.0")
    return cut_mod.Cutter(
        config=config,
        runner=runner,
        checkouts=checkouts,
        out_dir=tmp_path / "build",
        report=lambda line: runner.report_lines.append(line),
        **kw,
    )


# -- dry run ---------------------------------------------------------------


def test_dry_run_plans_without_building_tagging_or_publishing(config, checkouts, tmp_path, capsys):
    handler = make_handler(heads={"Snowline": PLATFORM_SHA, "snowline-pm": PM_SHA})
    runner = FakeRunner(handler, dry_run=True)
    plan = cut_mod.cut(
        config, "v0.1.0",
        checkouts=checkouts, out_dir=tmp_path / "build", runner=runner,
    )
    assert plan.components_to_build == ("platform", "pm")
    assert runner.runs == []
    assert not runner.did_read("uv", "build")
    assert not runner.did_read("git", "worktree")
    assert not runner.did_read("gh", "release", "create")
    assert "dry run" in capsys.readouterr().out


def test_preflight_refuses_a_dirty_checkout_before_anything_happens(config, checkouts, tmp_path):
    runner = FakeRunner(make_handler(dirty=True))
    with pytest.raises(m.ReleaseError, match="working tree is dirty"):
        cut_mod.cut(config, "v0.1.0", checkouts=checkouts, out_dir=tmp_path / "b", runner=runner)
    assert runner.runs == []


def test_preflight_refuses_an_unpushed_head(config, checkouts, tmp_path):
    runner = FakeRunner(make_handler(pushed=False))
    with pytest.raises(m.ReleaseError, match="not on origin/main"):
        cut_mod.cut(config, "v0.1.0", checkouts=checkouts, out_dir=tmp_path / "b", runner=runner)


def test_preflight_ignores_the_train_manifest_the_cutter_itself_wrote(config, checkouts, tmp_path):
    """A cut writes release/train.json into the platform checkout as its last
    step. If that alone counted as "dirty", the re-run path would be blocked by
    the cutter's own output."""
    inner = make_handler(heads={"Snowline": PLATFORM_SHA, "snowline-pm": PM_SHA})

    def handler(argv, cwd):
        if argv[:2] == ["git", "status"] and Path(cwd).name == "Snowline":
            return " M release/train.json\n"
        return inner(argv, cwd)

    runner = FakeRunner(handler, dry_run=True)
    plan = cut_mod.cut(config, "v0.1.0", checkouts=checkouts, out_dir=tmp_path / "b", runner=runner)
    assert plan.version == "v0.1.0"


def test_a_dirty_source_file_still_refuses_even_next_to_the_manifest(config, checkouts, tmp_path):
    inner = make_handler()

    def handler(argv, cwd):
        if argv[:2] == ["git", "status"] and Path(cwd).name == "Snowline":
            return " M release/train.json\n M src/snowline_platform/app.py\n"
        return inner(argv, cwd)

    runner = FakeRunner(handler)
    with pytest.raises(m.ReleaseError, match="dirty"):
        cut_mod.cut(config, "v0.1.0", checkouts=checkouts, out_dir=tmp_path / "b", runner=runner)


def test_missing_gh_auth_refuses_the_cut(config, checkouts, tmp_path):
    def handler(argv, cwd):
        if argv[:3] == ["gh", "auth", "status"]:
            return runner_mod.CommandError(argv, 1, "not logged in")
        return make_handler()(argv, cwd)

    runner = FakeRunner(handler)
    with pytest.raises(m.ReleaseError, match="gh is not authenticated"):
        cut_mod.cut(config, "v0.1.0", checkouts=checkouts, out_dir=tmp_path / "b", runner=runner)


# -- tests run in each repo's own environment ------------------------------


def test_component_tests_run_in_that_components_checkout(config, checkouts, tmp_path):
    """pm's suite must execute against pm's own venv and lock, never the
    platform's — `uv run` resolves the project from cwd."""
    runner = FakeRunner(make_handler())
    cutter = _cutter(config, checkouts, runner, tmp_path)
    cutter.run_tests(config.component("pm"))
    invocations = [(argv, cwd) for argv, cwd in runner.reads if argv[:2] == ("uv", "run")]
    assert invocations == [(("uv", "run", "python", "-m", "pytest", "-q"), checkouts["pm"])]


def test_skip_tests_runs_nothing(config, checkouts, tmp_path):
    runner = FakeRunner(make_handler())
    cutter = _cutter(config, checkouts, runner, tmp_path, skip_tests=True)
    cutter.run_tests(config.component("pm"))
    assert not runner.did_read("uv", "run")


# -- lock exports + the SDK-pin rewrite ------------------------------------

PM_EXPORT = (
    "httpx==0.28.1 \\\n    --hash=sha256:aaa\n    # via snowline-pm\n"
    "snowline-plugin-sdk @ git+https://github.com/snowlinedev/Snowline.git@"
    "8adaeb3774041efda8e8fae9d05c12ef77d4abea#subdirectory=sdk\n"
    "    # via snowline-pm\n"
)


def test_pm_lock_export_lands_with_no_git_dependency(config, checkouts, tmp_path):
    """Risk #6's acceptance in file form: the asset a pm venv is built from
    must not mention git, or the install is back to being from source."""
    runner = FakeRunner(lambda argv, cwd: PM_EXPORT if argv[:2] == ["uv", "export"] else "")
    cutter = _cutter(config, checkouts, runner, tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    cutter._export_locks(config.component("pm"), tmp_path / "tree", dist, "v0.1.0")

    text = (dist / "requirements-pm.txt").read_text()
    assert "git+" not in text
    assert "snowline-plugin-sdk==0.1.0" in text
    assert any("SDK pin rewritten (risk #6)" in line for line in runner.report_lines)


def test_platform_lock_exports_are_per_package_and_untouched(config, checkouts, tmp_path):
    runner = FakeRunner(lambda argv, cwd: "alembic==1.18.5\n" if argv[:2] == ["uv", "export"] else "")
    cutter = _cutter(config, checkouts, runner, tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    cutter._export_locks(config.component("platform"), tmp_path / "tree", dist, "v0.1.0")

    # §2: one export per SERVICE, `--package` scoped, workspace members dropped.
    exported = [argv for argv, _ in runner.reads if argv[:2] == ("uv", "export")]
    assert len(exported) == 3  # platform, governance, memory — the SDK is a library
    for argv in exported:
        assert "--frozen" in argv and "--no-emit-workspace" in argv and "--package" in argv
    assert sorted(p.name for p in dist.iterdir()) == [
        "requirements-governance.txt", "requirements-memory.txt", "requirements-platform.txt",
    ]


def test_a_missing_sdk_pin_in_pms_export_is_a_hard_failure(config, checkouts, tmp_path):
    """If pm's export stops containing the SDK at all, something changed that
    the cutter must not paper over by publishing anyway."""
    runner = FakeRunner(lambda argv, cwd: "httpx==0.28.1\n" if argv[:2] == ["uv", "export"] else "")
    cutter = _cutter(config, checkouts, runner, tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    with pytest.raises(m.ReleaseError, match="no snowline-plugin-sdk requirement"):
        cutter._export_locks(config.component("pm"), tmp_path / "tree", dist, "v0.1.0")


# -- pruning ---------------------------------------------------------------


def test_prune_drops_sdists_and_the_wheel_that_never_ships(config, checkouts, tmp_path):
    dist = tmp_path / "build" / "dist" / "platform"
    dist.mkdir(parents=True)
    for name in [
        "snowline_platform-0.1.0-py3-none-any.whl",
        "snowline_governance-0.1.0-py3-none-any.whl",
        "snowline_memory-0.1.0-py3-none-any.whl",
        "snowline_plugin_sdk-0.1.0-py3-none-any.whl",
        "snowline_remote_front-0.1.0-py3-none-any.whl",
        "snowline_platform-0.1.0.tar.gz",
    ]:
        (dist / name).touch()
    runner = FakeRunner(make_handler())
    cutter = _cutter(config, checkouts, runner, tmp_path)
    cutter._prune(config.component("platform"), dist, "v0.1.0")

    assert sorted(p.name for p in dist.iterdir()) == [
        "snowline_governance-0.1.0-py3-none-any.whl",
        "snowline_memory-0.1.0-py3-none-any.whl",
        "snowline_platform-0.1.0-py3-none-any.whl",
        "snowline_plugin_sdk-0.1.0-py3-none-any.whl",
    ]


def test_prune_fails_loudly_when_an_expected_wheel_was_not_built(config, checkouts, tmp_path):
    dist = tmp_path / "build" / "dist" / "pm"
    dist.mkdir(parents=True)
    runner = FakeRunner(make_handler())
    cutter = _cutter(config, checkouts, runner, tmp_path)
    with pytest.raises(m.ReleaseError, match="expected wheel snowline_pm-0.1.0"):
        cutter._prune(config.component("pm"), dist, "v0.1.0")


# -- publishing, and re-running after a partial failure --------------------


def _pm_dist(cutter, config, version="v0.1.0") -> Path:
    dist = cutter.dist_dir(config.component("pm"))
    dist.mkdir(parents=True, exist_ok=True)
    (dist / m.wheel_filename("snowline-pm", version)).touch()
    (dist / "requirements-pm.txt").touch()
    return dist


def test_publish_tags_pushes_and_creates_the_release_with_its_own_assets(config, checkouts, tmp_path):
    runner = FakeRunner(make_handler(heads={"snowline-pm": PM_SHA}))
    cutter = _cutter(config, checkouts, runner, tmp_path)
    cutter.out_dir.mkdir(parents=True, exist_ok=True)
    _pm_dist(cutter, config)
    plan = m.plan_train(config, "v0.1.0", heads={"platform": PLATFORM_SHA, "pm": PM_SHA})

    cutter.publish(config.component("pm"), plan, "v0.1.0")

    assert runner.ran("git", "tag")[0][:4] == ("git", "tag", "-a", "v0.1.0")
    assert runner.ran("git", "push") == [("git", "push", "origin", "v0.1.0")]
    created = runner.ran("gh", "release", "create")[0]
    assert "--repo" in created and "snowlinedev/snowline-pm" in created
    # Only pm's own assets — nothing from the platform release.
    assets = [a for a in created if a.endswith((".whl", ".txt"))]
    assert sorted(Path(a).name for a in assets) == [
        "requirements-pm.txt", "snowline_pm-0.1.0-py3-none-any.whl",
    ]
    notes = (cutter.out_dir / "notes-pm.md").read_text()
    assert "Something landed (#42)" in notes


def test_publish_skips_a_tag_already_pushed_at_the_same_sha(config, checkouts, tmp_path):
    runner = FakeRunner(make_handler(
        heads={"snowline-pm": PM_SHA}, tags={("snowline-pm", "v0.1.0"): PM_SHA},
    ))
    cutter = _cutter(config, checkouts, runner, tmp_path)
    cutter.out_dir.mkdir(parents=True, exist_ok=True)
    _pm_dist(cutter, config)
    plan = m.plan_train(config, "v0.1.0", heads={"platform": PLATFORM_SHA, "pm": PM_SHA})

    cutter.publish(config.component("pm"), plan, "v0.1.0")

    assert runner.ran("git", "tag") == []
    assert runner.ran("git", "push") == []
    # The release did not exist yet, so the resumed cut still creates it.
    assert len(runner.ran("gh", "release", "create")) == 1
    assert any("already on" in line for line in runner.report_lines)


def test_publish_refreshes_assets_instead_of_duplicating_an_existing_release(config, checkouts, tmp_path):
    runner = FakeRunner(make_handler(
        heads={"snowline-pm": PM_SHA},
        tags={("snowline-pm", "v0.1.0"): PM_SHA},
        releases={("snowlinedev/snowline-pm", "v0.1.0")},
    ))
    cutter = _cutter(config, checkouts, runner, tmp_path)
    cutter.out_dir.mkdir(parents=True, exist_ok=True)
    _pm_dist(cutter, config)
    plan = m.plan_train(config, "v0.1.0", heads={"platform": PLATFORM_SHA, "pm": PM_SHA})

    cutter.publish(config.component("pm"), plan, "v0.1.0")

    assert runner.ran("gh", "release", "create") == []
    uploaded = runner.ran("gh", "release", "upload")[0]
    assert "--clobber" in uploaded


def test_publish_refuses_to_move_a_tag_that_points_somewhere_else(config, checkouts, tmp_path):
    runner = FakeRunner(make_handler(
        heads={"snowline-pm": PM_SHA}, tags={("snowline-pm", "v0.1.0"): "9" * 40},
    ))
    cutter = _cutter(config, checkouts, runner, tmp_path)
    cutter.out_dir.mkdir(parents=True, exist_ok=True)
    _pm_dist(cutter, config)
    plan = m.plan_train(config, "v0.1.0", heads={"platform": PLATFORM_SHA, "pm": PM_SHA})

    with pytest.raises(m.ReleaseError, match="Refusing to move a published tag"):
        cutter.publish(config.component("pm"), plan, "v0.1.0")
    assert runner.ran("git", "push") == []


# -- respin: the carried component's wheels come from its recorded tag -----


def test_respin_fetches_carried_forward_wheels_from_the_manifest_tag(config, checkouts, tmp_path):
    """A pm respin does not rebuild the platform, so the SDK wheel pm installs
    has to come from the platform release the manifest still points at — which
    is exactly how the target machine gets it."""
    runner = FakeRunner(make_handler())
    cutter = _cutter(config, checkouts, runner, tmp_path)
    previous = m.plan_train(config, "v0.1.0", heads={"platform": PLATFORM_SHA, "pm": PM_SHA}).to_manifest()
    plan = m.plan_train(
        config, "v0.1.1",
        heads={"platform": PLATFORM_SHA, "pm": "c" * 40},
        previous=previous, respin="pm",
    )
    _pm_dist(cutter, config, "v0.1.1")

    dirs = cutter._find_links(plan)

    download = next(
        argv for argv, _ in runner.reads if argv[:3] == ("gh", "release", "download")
    )
    assert "v0.1.0" in download and "snowlinedev/Snowline" in download
    assert "*.whl" in download
    assert len(dirs) == 2


def test_respin_publishes_a_manifest_carrier_platform_release(config, checkouts, tmp_path):
    """A respin of pm still ends in a PLATFORM release tagged with the train
    version and carrying train.json — spec §4's 'latest' resolution reads the
    manifest off the platform repo's latest release, so without this the
    respun train is invisible to every target (#207 review). The platform is
    tagged at its CARRIED-FORWARD sha; the release carries the manifest only."""
    runner = FakeRunner(make_handler())
    cutter = _cutter(config, checkouts, runner, tmp_path)
    previous = m.plan_train(
        config, "v0.1.0", heads={"platform": PLATFORM_SHA, "pm": PM_SHA}
    ).to_manifest()
    plan = m.plan_train(
        config, "v0.1.1",
        heads={"pm": "c" * 40},
        previous=previous, respin="pm",
    )
    cutter.write_manifest(plan)

    cutter.publish_manifest_only(plan, "v0.1.1")

    tag = runner.ran("git", "tag", "-a")[0]
    assert tag[3] == "v0.1.1" and tag[4] == PLATFORM_SHA  # carried sha
    create = runner.ran("gh", "release", "create")[0]
    assert create[3] == "v0.1.1" and "snowlinedev/Snowline" in create
    # the manifest is the ONLY asset
    assets = [a for a in create if a.endswith(".json")]
    assert len(assets) == 1 and assets[0].endswith("train.json")
    assert not any(a.endswith(".whl") for a in create)


def test_sdk_train_version_uses_the_carried_tag_on_a_respin(config):
    """Pinning pm's rewritten SDK requirement to the CUT version on a respin
    would name a wheel that exists in no release's assets — the pin must come
    from the SDK's own (carried-forward) plan entry (#207 review)."""
    previous = m.plan_train(
        config, "v0.1.0", heads={"platform": PLATFORM_SHA, "pm": PM_SHA}
    ).to_manifest()
    plan = m.plan_train(
        config, "v0.1.1",
        heads={"pm": "c" * 40},
        previous=previous, respin="pm",
    )
    assert m.sdk_train_version(config, plan) == "0.1.0"
    minor = m.plan_train(
        config, "v0.2.0", heads={"platform": PLATFORM_SHA, "pm": PM_SHA}
    )
    assert m.sdk_train_version(config, minor) == "0.2.0"


def test_publish_deletes_a_stale_local_tag_before_retagging(config, checkouts, tmp_path):
    """A local tag left by a run whose push failed points at the wrong sha;
    pushing it as-is would publish a tag that lies about what was released
    (#207 review). Origin has no tag (tag_decision said create), so the stale
    local one is deleted and recreated at the blessed sha."""
    stale = "d" * 40
    runner = FakeRunner(make_handler(
        heads={"snowline-pm": PM_SHA},
        tags={("snowline-pm", "local:v0.1.0"): stale},
    ))
    cutter = _cutter(config, checkouts, runner, tmp_path)
    cutter.out_dir.mkdir(parents=True, exist_ok=True)
    _pm_dist(cutter, config)
    plan = m.plan_train(config, "v0.1.0", heads={"platform": PLATFORM_SHA, "pm": PM_SHA})

    cutter.publish(config.component("pm"), plan, "v0.1.0")

    assert runner.ran("git", "tag", "-d") == [("git", "tag", "-d", "v0.1.0")]
    retag = runner.ran("git", "tag", "-a")[0]
    assert retag[3] == "v0.1.0" and retag[4] == PM_SHA


def test_write_manifest_does_not_touch_the_checkout_until_recorded(config, checkouts, tmp_path):
    """The checkout train record arms plan_train's immutability check, so a
    cut that fails before publishing must leave it unwritten — the advertised
    re-run-at-the-same-version recovery depends on it (#207 review).
    `write_manifest` produces only the release asset; `record_manifest` (run
    after publish) writes the record."""
    runner = FakeRunner(make_handler())
    cutter = _cutter(config, checkouts, runner, tmp_path)
    plan = m.plan_train(config, "v0.1.0", heads={"platform": PLATFORM_SHA, "pm": PM_SHA})

    asset = cutter.write_manifest(plan)

    assert asset.exists()
    checked_in = checkouts["platform"] / cut_mod.MANIFEST_RELPATH
    assert not checked_in.exists()
    cutter.record_manifest(plan)
    assert checked_in.exists()
    assert json.loads(checked_in.read_text())["version"] == "v0.1.0"


def test_resolve_checkout_refuses_a_config_outside_the_release_dir(config, tmp_path):
    """`--config ~/somewhere/copy.json` must not silently resolve components
    against the config's grandparent directory (#207 review) — overrides are
    the escape hatch, and the refusal says so."""
    from dataclasses import replace

    moved = replace(config, source=tmp_path / "copy.json")
    with pytest.raises(m.ReleaseError, match="--checkout"):
        m.resolve_checkout(moved, config.components[0], {})
    # an override still works without root derivation
    path = m.resolve_checkout(
        moved, config.components[0], {config.components[0].name: str(tmp_path / "co")}
    )
    assert path == (tmp_path / "co").resolve()


# -- status ----------------------------------------------------------------


def test_status_reports_the_train_and_what_a_cut_would_pick_up(config, checkouts, tmp_path, capsys):
    manifest = m.plan_train(
        config, "v0.1.0", heads={"platform": PLATFORM_SHA, "pm": PM_SHA}
    ).to_manifest()
    (checkouts["platform"] / "release").mkdir(parents=True)
    (checkouts["platform"] / cut_mod.MANIFEST_RELPATH).write_text(json.dumps(manifest))
    # pm has moved on since the train; the platform has not.
    runner = FakeRunner(make_handler(heads={"Snowline": PLATFORM_SHA, "snowline-pm": "c" * 40}))

    cut_mod.status(config, checkouts=checkouts, runner=runner, report=print)

    out = capsys.readouterr().out
    assert "train: v0.1.0" in out
    assert "would be rebuilt at HEAD" in out
    governance_row = next(ln for ln in out.splitlines() if ln.startswith("governance"))
    assert "up to date" in governance_row


def test_status_says_so_when_no_train_has_been_cut(config, checkouts, tmp_path, capsys):
    runner = FakeRunner(make_handler())
    cut_mod.status(config, checkouts=checkouts, runner=runner, report=print)
    assert "train: none yet" in capsys.readouterr().out


# -- milestone gate (#242) ---------------------------------------------------

from snowline_platform.release import gate as gate_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_pm(monkeypatch):
    """No test may dial a live platform: the default fetcher reports pm down."""

    def down(milestone):
        raise gate_mod.GateUnavailable("test: no pm")

    monkeypatch.setattr(gate_mod, "fetch_milestone_status", down)


def _pm_status(count=2, achievable=False, registry=True, **extra):
    body = {
        "registry": {"status": "open"} if registry else None,
        "completion": {
            "achievable": achievable,
            "required_remaining": {
                "count": count,
                "items": [
                    {"id": f"{i:08x}-0000", "title": f"required item {i}"} for i in range(count)
                ],
            },
            "blockers": {"blocked_by_cancelled": ["deadbeef"], "stale_criteria": ["c1"]},
        },
        "readiness_summary": "2 of 5 required open",
    }
    body.update(extra)
    return body


def _gate_cut(config, checkouts, tmp_path, fetch, *, dry_run=True, force=False):
    handler = make_handler(heads={"Snowline": PLATFORM_SHA, "snowline-pm": PM_SHA})
    runner = FakeRunner(handler, dry_run=dry_run)
    plan = cut_mod.cut(
        config, "v0.1.0", checkouts=checkouts, out_dir=tmp_path / "build",
        runner=runner, force=force, fetch_status=fetch, report=runner.report_lines.append,
    )
    return plan, runner


def test_version_to_milestone_mapping():
    assert gate_mod.train_milestone("v0.5.0") == "snowlinedev/v0.5"
    assert gate_mod.train_milestone("v0.5.1") == "snowlinedev/v0.5"
    assert gate_mod.train_milestone("v1.0.0") == "snowlinedev/v1.0"


def test_cut_refuses_on_open_required_items(config, checkouts, tmp_path):
    seen = []

    def fetch(ms):
        seen.append(ms)
        return _pm_status()

    handler = make_handler(heads={"Snowline": PLATFORM_SHA, "snowline-pm": PM_SHA})
    runner = FakeRunner(handler)
    with pytest.raises(m.ReleaseError, match="2 required item"):
        cut_mod.cut(
            config, "v0.1.0", checkouts=checkouts, out_dir=tmp_path / "build",
            runner=runner, fetch_status=fetch, report=runner.report_lines.append,
        )
    assert seen == ["snowlinedev/v0.1"]
    out = "\n".join(runner.report_lines)
    assert "00000000  required item 0" in out
    assert "blocked_by_cancelled" in out and "stale criteria" in out
    assert "readiness: 2 of 5 required open" in out
    assert runner.runs == [] and not runner.did_read("uv", "build")


def test_cut_force_warns_and_records_gated(config, checkouts, tmp_path):
    plan, runner = _gate_cut(config, checkouts, tmp_path, lambda ms: _pm_status(), force=True)
    assert plan.gated == {"milestone": "snowlinedev/v0.1", "forced": True, "required_remaining": 2}
    assert json.loads(m.render_manifest(plan))["gated"]["forced"] is True
    assert "--force given" in "\n".join(runner.report_lines)


def test_cut_proceeds_when_milestone_unresolved_with_warning(config, checkouts, tmp_path):
    plan, runner = _gate_cut(config, checkouts, tmp_path, lambda ms: _pm_status(registry=False))
    assert "skipped" in plan.gated and "resolve" in plan.gated["skipped"]
    assert "WARNING" in "\n".join(runner.report_lines)


def test_cut_proceeds_when_pm_unreachable_with_warning(config, checkouts, tmp_path):
    def boom(ms):
        raise ConnectionError("refused")

    plan, runner = _gate_cut(config, checkouts, tmp_path, boom)
    assert "pm unreachable" in plan.gated["skipped"]
    assert "NOT gated" in "\n".join(runner.report_lines)


def test_dry_run_reports_gate(config, checkouts, tmp_path):
    plan, runner = _gate_cut(config, checkouts, tmp_path, lambda ms: _pm_status())
    out = "\n".join(runner.report_lines)
    assert "milestone gate (snowlinedev/v0.1)" in out
    assert "a real cut would REFUSE" in out
    assert runner.runs == []
    ok, _ = _gate_cut(config, checkouts, tmp_path, lambda ms: _pm_status(0, True))
    assert ok.gated["forced"] is False
