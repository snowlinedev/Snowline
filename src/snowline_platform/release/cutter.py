"""The release cutter — `snowline release cut`, run HOST-SIDE by the operator.

Why host-side and not a workflow per repo: item #202 constrains this outright —
"no GitHub Actions in plugin repos — if the pipeline needs CI, it lives with
the platform repo's public workflow posture or runs host-side". The pm repo is
private and gets no workflows, and a cross-repo Actions cut would need a PAT
crossing the public/private boundary — exactly the "PAT gymnastics" the spec
§4 rejects. The operator's machine already has every checkout, `gh` auth for
both repos, node for the dashboard, and Postgres 16 for the smoke tests, so the
cutter runs there. The spec's §4 mechanics are unchanged: per-component tags,
per-repo releases carrying only their own repo's wheels, and the manifest on
the platform release. Only the executor moved.

The order of operations is load-bearing:

  preflight (refuse on anything that would make a tag lie)
    -> plan the train (respin carry-forward, immutability check)
    -> per component: throwaway git worktree AT THE BLESSED SHA
         -> lock exports  (BEFORE stamping: `uv export --frozen` validates the
            lock against pyproject, and a stamped version would trip it)
         -> stamp versions -> uv build -> prune
    -> dashboard dist (built in the real checkout, see `_build_dashboard`)
    -> write the manifest
    -> smoke tests from the built wheels against empty scratch databases
    -> tag, push, publish

Nothing is tagged or published until every build and every smoke test has
passed, and every publishing step is a no-op when it has already happened —
`snowline release cut` is safe to re-run after a partial failure.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import model as m
from . import runner as r
from .model import Component, ReleaseConfig, ReleaseError, Service, TrainPlan

SMOKE_DB_PREFIX = "snowline_smoke_"
MANIFEST_RELPATH = Path("release/train.json")


@dataclass
class Cutter:
    config: ReleaseConfig
    runner: r.Runner
    checkouts: Mapping[str, Path]
    out_dir: Path
    report: r.Report = print
    skip_tests: bool = False
    skip_smoke: bool = False
    sdk_version: str | None = None  # the SDK wheel version THIS train carries
    notes: list[str] = field(default_factory=list)

    # -- paths ------------------------------------------------------------

    def checkout(self, comp: Component) -> Path:
        return self.checkouts[comp.name]

    def dist_dir(self, comp: Component) -> Path:
        return self.out_dir / "dist" / comp.name

    def carried_dir(self, comp: Component) -> Path:
        """Where a NON-rebuilt component's downloaded wheels land on a respin.

        Deliberately not `dist_dir`: dist is what `publish` uploads, and a
        manifest-only platform release on a respin must carry the manifest
        alone — mixing downloaded old-version wheels into dist would re-upload
        them under the new tag (#207 review)."""
        return self.out_dir / "carried" / comp.name

    # -- 1. preflight -----------------------------------------------------

    def preflight(self, components: list[Component]) -> dict[str, m.CheckoutState]:
        self.report("preflight")
        states: dict[str, m.CheckoutState] = {}
        refusals: list[str] = []
        for comp in components:
            path = self.checkout(comp)
            # The cutter writes `release/train.json` into the manifest
            # component's checkout as the last step of a cut. Ignoring it here
            # is what makes a re-run after a partial failure possible without
            # asking the operator to stash the very file we just wrote.
            ignore = (str(MANIFEST_RELPATH),) if comp.carries_manifest else ()
            state = _state(self.runner, comp.name, path, ignore)
            states[comp.name] = state
            issues = m.preflight_issues(state)
            for warning in m.preflight_warnings(state):
                self.report(f"  ! {comp.name}: {warning}")
            if issues:
                refusals.extend(f"{comp.name}: {issue}" for issue in issues)
            else:
                self.report(
                    f"  ok {comp.name}: main @ {(state.head or '')[:12]} "
                    f"({state.path})"
                )
        if not r.gh_auth_ok(self.runner):
            refusals.append(
                "gh is not authenticated (`gh auth status`) — the pm repo is "
                "private and every release step needs it"
            )
        if refusals:
            raise ReleaseError(
                "preflight refused this cut:\n  - " + "\n  - ".join(refusals)
            )
        return states

    # -- 2. tests ---------------------------------------------------------

    def run_tests(self, comp: Component) -> None:
        if self.skip_tests:
            self.report(f"  tests skipped ({comp.name}) — --skip-tests")
            return
        if not comp.test:
            self.report(f"  no test command configured for {comp.name}")
            return
        self.report(f"  tests: {' '.join(comp.test)} (in {self.checkout(comp)})")
        # Run in the component's OWN checkout so `uv run` picks that project's
        # venv and lock — pm's suite must never execute against the platform's
        # environment.
        self.runner.read(list(comp.test), cwd=self.checkout(comp), env=dict(comp.test_env))
        self.report(f"  tests passed ({comp.name})")

    # -- 3. build ---------------------------------------------------------

    def build(self, comp: Component, sha: str, version: str) -> None:
        dist = self.dist_dir(comp)
        if dist.exists():
            shutil.rmtree(dist)
        dist.mkdir(parents=True, exist_ok=True)
        with _throwaway_worktree(self.runner, self.checkout(comp), sha, self.report) as tree:
            self._export_locks(comp, tree, dist, version)
            self._stamp(comp, tree, version)
            self._build_wheels(comp, tree, dist)
            self._prune(comp, dist, version)
            if comp.dashboard:
                self._build_dashboard(comp, tree, dist, version)

    def _export_locks(self, comp: Component, tree: Path, dist: Path, version: str) -> None:
        """Per-service `requirements-<service>.txt` (spec §2).

        `--no-emit-workspace` is what keeps workspace members out as
        unresolvable local path entries; they come back as wheel pins from the
        release assets. Run BEFORE stamping — `--frozen` checks the lock
        against pyproject, and a stamped version invalidates that check.
        """
        for svc in comp.services:
            if not svc.is_service:
                continue
            argv = ["uv", "export", "--frozen", "--no-emit-workspace"]
            if comp.build == "workspace":
                argv += ["--package", svc.package]
            text = self.runner.read(argv, cwd=tree)
            if comp.rewrite_sdk_pin:
                # Pin to the SDK wheel THIS train carries — on a respin that
                # is the carried-forward version, not the cut version (#207
                # review; `m.sdk_train_version` is the single source).
                assert self.sdk_version is not None
                text = self._rewrite_sdk_pin(comp, svc, text, self.sdk_version)
            target = dist / m.requirements_filename(svc.name)
            target.write_text(text)
            self.report(f"  lock export: {target.name}")

    def _rewrite_sdk_pin(self, comp: Component, svc: Service, text: str, version: str) -> str:
        result = m.rewrite_sdk_pin(text, version)
        if not result.found:
            raise ReleaseError(
                f"{comp.name}/{svc.name} is configured with `rewrite_sdk_pin` but its "
                f"lock export contains no {m.SDK_PACKAGE} requirement — the export is "
                "not what this cutter thinks it is; investigate before publishing"
            )
        if result.was_git_pin:
            self.report(
                f"  SDK pin rewritten (risk #6): {m.SDK_PACKAGE} @ git+…@"
                f"{result.git_rev[:12]} -> =={m.pep440(version)}"
            )
            self.notes.append(
                f"{comp.name}: SDK git pin {result.git_rev[:12]} rewritten to the "
                f"train's SDK wheel {m.pep440(version)}"
            )
        else:
            self.report(f"  SDK pin already a version pin; repinned to {m.pep440(version)}")
        return result.text

    def _stamp(self, comp: Component, tree: Path, version: str) -> None:
        """Give the wheels the train version without touching the repos.

        The pyprojects all say `0.0.1`; a train's wheels must say `0.1.0`. The
        rewrite happens inside a throwaway git worktree checked out at the
        blessed sha, so the version never exists as a commit, never dirties the
        operator's checkout, and cannot leak even if the cut dies mid-build —
        the worktree is removed either way. The alternative (a dynamic
        env-driven hatchling version) would need a `dynamic = ["version"]`
        source added to five pyprojects across two repos, permanently, to serve
        one command.
        """
        for pyproject in _stamp_targets(comp, tree):
            pyproject.write_text(
                m.stamp_pyproject_version(pyproject.read_text(), version)
            )
        self.report(f"  stamped {comp.name} pyprojects at {m.pep440(version)}")

    def _build_wheels(self, comp: Component, tree: Path, dist: Path) -> None:
        argv = ["uv", "build", "--out-dir", str(dist)]
        if comp.build == "workspace":
            argv.append("--all-packages")
        self.runner.read(argv, cwd=tree)
        self.report(f"  built wheels for {comp.name}")

    def _prune(self, comp: Component, dist: Path, version: str) -> None:
        """Drop sdists and the wheels that never ship.

        `uv build --all-packages` also builds `snowline-remote-front` (a
        workspace member, Fly.io deployment surface) — spec §2 is explicit that
        it never ships in this release.
        """
        for path in sorted(dist.iterdir()):
            # `uv build --out-dir` drops a `.gitignore` in the output dir. It is
            # not an artifact and must never reach a release's asset list.
            if path.name.startswith("."):
                path.unlink()
                continue
            # sdists: built alongside the wheels, never shipped (§2 installs
            # wheels only). Runs before the dashboard tarball is created.
            if path.name.endswith(".tar.gz"):
                path.unlink()
                continue
            if any(path.name.startswith(prefix) for prefix in comp.prune_wheels):
                path.unlink()
                self.report(f"  pruned {path.name} (never ships — spec §2)")
        for svc in comp.services:
            expected = dist / m.wheel_filename(svc.package, version)
            if not expected.exists():
                raise ReleaseError(
                    f"expected wheel {expected.name} was not produced by the "
                    f"{comp.name} build (found: "
                    f"{sorted(p.name for p in dist.iterdir())})"
                )

    def _build_dashboard(self, comp: Component, tree: Path, dist: Path, version: str) -> None:
        """`npm run build` -> `dashboard-dist-<v>.tar.gz` (spec §2.2).

        Built in the THROWAWAY worktree, same isolation as the wheels: the
        preflight's `git status` proves tracked files clean but says nothing
        about ignored ones — a `dashboard/.env.local` in the real checkout
        would get its VITE_* values baked into the shipped bundle (#207
        review), an artifact that no longer corresponds to the blessed sha.
        The `npm ci` this costs per cut is the price of a tarball that is a
        pure function of the tag. The npm build carries its own gates
        (`validate:tokens`, `tsc -b`).
        """
        assert comp.dashboard is not None
        dash = tree / comp.dashboard.dir
        if comp.dashboard.install:
            self.report(f"  dashboard: {' '.join(comp.dashboard.install)} (in {dash})")
            self.runner.read(list(comp.dashboard.install), cwd=dash)
        self.report(f"  dashboard: {' '.join(comp.dashboard.build)} (in {dash})")
        self.runner.read(list(comp.dashboard.build), cwd=dash)
        out = dist / m.dashboard_tarball_name(version)
        self.runner.read(
            ["tar", "-czf", str(out), "-C", str(dash), comp.dashboard.dist]
        )
        self.report(f"  dashboard dist: {out.name}")

    # -- 4. manifest ------------------------------------------------------

    def write_manifest(self, plan: TrainPlan) -> Path:
        """The RELEASE-ASSET copy only. The checkout copy is written by
        `record_manifest` AFTER a successful publish — writing it earlier
        armed `plan_train`'s immutability check against a cut that failed
        before publishing anything, wrongly blocking the advertised
        re-run-at-the-same-version recovery path (#207 review)."""
        comp = self.config.manifest_component
        asset = self.dist_dir(comp) / MANIFEST_RELPATH.name
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(m.render_manifest(plan))
        self.report(f"  manifest asset written: {asset}")
        return asset

    def record_manifest(self, plan: TrainPlan) -> None:
        """Write the checkout copy — the train RECORD — once publish succeeded."""
        comp = self.config.manifest_component
        checked_in = self.checkout(comp) / MANIFEST_RELPATH
        checked_in.parent.mkdir(parents=True, exist_ok=True)
        checked_in.write_text(m.render_manifest(plan))
        self.report(f"  train recorded: {checked_in}")
        self.notes.append(
            f"commit {MANIFEST_RELPATH} in {comp.repo} as the train record — the "
            "copy attached to the release is what sync reads"
        )

    # -- 5. smoke ---------------------------------------------------------

    def smoke(self, comp: Component, plan: TrainPlan, version: str) -> None:
        if self.skip_smoke:
            self.report(f"  smoke skipped ({comp.name}) — --skip-smoke")
            return
        find_links = self._find_links(plan)
        for svc in comp.services:
            if not svc.is_service or svc.boot is None:
                continue
            self._smoke_one(comp, svc, version, find_links)

    def _find_links(self, plan: TrainPlan) -> list[Path]:
        """Wheel sources for a smoke install: every component's assets.

        On a respin the non-respun components were not rebuilt, so their wheels
        are pulled from the tag the manifest carried forward — which is
        precisely how the target machine will get them, and how risk #6's
        acceptance ("a pm venv built from release assets alone") is exercised
        rather than assumed.
        """
        dirs: list[Path] = []
        for comp in self.config.components:
            dist = self.dist_dir(comp)
            if dist.exists() and any(dist.glob("*.whl")):
                dirs.append(dist)
                continue
            carried = [p for p in plan.for_component(comp.name) if not p.rebuilt]
            if not carried:
                continue
            # Downloaded carried wheels live OUTSIDE dist — dist is what
            # `publish` uploads, and old-version wheels must never ride a new
            # tag's release (#207 review).
            into = self.carried_dir(comp)
            into.mkdir(parents=True, exist_ok=True)
            tag = carried[0].tag
            self.report(f"  fetching {comp.name} wheels from {comp.repo} {tag} (carried forward)")
            self.runner.read(
                ["gh", "release", "download", tag, "--repo", comp.repo,
                 "--pattern", "*.whl", "--dir", str(into), "--clobber"]
            )
            dirs.append(into)
        return dirs

    def _smoke_one(self, comp: Component, svc: Service, version: str, find_links: list[Path]) -> None:
        assert svc.boot is not None
        db = f"{SMOKE_DB_PREFIX}{svc.name}"
        url = f"postgresql+psycopg:///{db}"
        work = self.out_dir / "smoke" / svc.name
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)
        venv = work / "venv"
        self.report(f"  smoke {svc.name}: fresh venv + empty {db}")
        self.runner.read(["uv", "venv", "--python", "3.12", str(venv)])
        python = venv / "bin" / "python"
        install = ["uv", "pip", "install", "--python", str(python)]
        for link in find_links:
            install += ["--find-links", str(link)]
        # The snowline wheels are named by FILE PATH, never by package name:
        # name-based resolution would let PyPI (still in the path for the
        # lock's third-party deps) mask a missing release asset — or resolve a
        # name-squatted `snowline-*` package and execute it host-side (#207
        # review). `_wheel_path` hard-fails on a missing asset instead.
        wheels = [_wheel_path(find_links, m.wheel_filename(svc.package, version))]
        if comp.rewrite_sdk_pin:
            assert self.sdk_version is not None
            wheels.append(
                _wheel_path(find_links, m.wheel_filename(m.SDK_PACKAGE, self.sdk_version))
            )
        install += [str(w) for w in wheels]
        install += ["-r", str(self.dist_dir(comp) / m.requirements_filename(svc.name))]
        self.runner.read(install)
        driver = work / "_smoke_boot.py"
        shutil.copyfile(Path(__file__).with_name("_smoke_boot.py"), driver)
        spec = {
            "module": svc.boot.module,
            "factory": svc.boot.factory,
            "attr": svc.boot.attr,
            "kwargs": dict(svc.boot.kwargs),
            "database_url_env": svc.boot.database_url_env,
        }
        self.runner.read(["dropdb", "--if-exists", db])
        self.runner.read(["createdb", db])
        try:
            out = self.runner.read(
                [str(python), str(driver), json.dumps(spec)],
                cwd=work,
                env={svc.boot.database_url_env: url},
            )
            self.report(f"    {out.strip().splitlines()[-1] if out.strip() else 'smoke ok'}")
        finally:
            self.runner.read(["dropdb", "--if-exists", db], check=False)

    # -- 6. publish -------------------------------------------------------

    def _ensure_tag(self, comp: Component, sha: str, version: str) -> None:
        """Tag `version` -> `sha` on `comp`'s repo, idempotently and safely."""
        path = self.checkout(comp)
        existing = r.existing_tag_sha(self.runner, path, version)
        action = m.tag_decision(tag=version, target_sha=sha, existing_sha=existing)
        if action == "skip":
            self.report(f"  tag {version} already on {comp.repo} at {sha[:12]} — skipping")
            return
        # A LOCAL tag left by a run whose push failed may point at a stale
        # sha; `git tag -a` would silently no-op under check=False and the
        # unconditional push would then publish the STALE tag — a tag that
        # lies about what was released (#207 review). Origin has no such
        # tag (action != "skip"), so deleting the local leftover is safe.
        local = r.local_tag_sha(self.runner, path, version)
        if local is not None and local != sha:
            self.report(
                f"  stale local tag {version} @ {local[:12]} (failed prior "
                f"run) — deleting before re-tagging at {sha[:12]}"
            )
            self.runner.run(["git", "tag", "-d", version], cwd=path)
        self.report(f"  tagging {comp.repo} {version} -> {sha[:12]}")
        if local is None or local != sha:
            self.runner.run(
                ["git", "tag", "-a", version, sha, "-m", f"Snowline train {version}"],
                cwd=path,
            )
        self.runner.run(["git", "push", "origin", version], cwd=path)

    def publish(self, comp: Component, plan: TrainPlan, version: str) -> None:
        path = self.checkout(comp)
        plans = plan.for_component(comp.name)
        sha = plans[0].sha
        self._ensure_tag(comp, sha, version)

        assets = sorted(
            str(p) for p in self.dist_dir(comp).iterdir()
            if p.is_file() and not p.name.startswith(".")
        )
        if r.release_exists(self.runner, comp.repo, version):
            self.report(f"  release {comp.repo} {version} exists — refreshing assets")
            if assets:
                self.runner.run(
                    ["gh", "release", "upload", version, *assets,
                     "--repo", comp.repo, "--clobber"]
                )
            return
        prev = r.previous_tag(self.runner, path, sha)
        subjects = r.commit_subjects(self.runner, path, prev, sha)
        notes = m.format_release_notes(
            component=comp.name,
            repo=comp.repo,
            version=version,
            previous_tag=prev,
            subjects=subjects,
            assets=[Path(a).name for a in assets],
        )
        notes_file = self.out_dir / f"notes-{comp.name}.md"
        notes_file.write_text(notes)
        self.report(f"  creating release {comp.repo} {version} with {len(assets)} asset(s)")
        self.runner.run(
            ["gh", "release", "create", version, *assets,
             "--repo", comp.repo,
             "--title", f"Snowline {version} — {comp.name}",
             "--notes-file", str(notes_file)]
        )

    def publish_manifest_only(self, plan: TrainPlan, version: str) -> None:
        """Respin publishing for the manifest component when it was NOT rebuilt.

        Spec §4: the installer resolves "latest" from the platform repo's
        latest release and pins everything off the manifest inside it — so
        EVERY train, respins included, must end in a platform release carrying
        the new train.json, or the respun train is invisible to every target
        (#207 review). The platform repo is tagged `version` at its
        carried-forward sha (nothing rebuilt — its wheels stay on their
        manifest-recorded tag), and the release carries the manifest alone.
        """
        comp = self.config.manifest_component
        sha = plan.for_component(comp.name)[0].sha
        self._ensure_tag(comp, sha, version)
        asset = self.dist_dir(comp) / MANIFEST_RELPATH.name
        if not asset.exists():
            raise ReleaseError(
                f"manifest asset missing at {asset} — write_manifest must run "
                "before publish"
            )
        if r.release_exists(self.runner, comp.repo, version):
            self.report(f"  release {comp.repo} {version} exists — refreshing manifest")
            self.runner.run(
                ["gh", "release", "upload", version, str(asset),
                 "--repo", comp.repo, "--clobber"]
            )
            return
        notes = (
            f"Train {version} — respin of `{plan.respin}`. This release exists "
            f"to carry `{MANIFEST_RELPATH.name}` (the train manifest); "
            f"{comp.name}'s own wheels were not rebuilt and live on the tag "
            "recorded in the manifest."
        )
        notes_file = self.out_dir / f"notes-{comp.name}.md"
        notes_file.write_text(notes)
        self.report(f"  creating manifest-carrier release {comp.repo} {version}")
        self.runner.run(
            ["gh", "release", "create", version, str(asset),
             "--repo", comp.repo,
             "--title", f"Snowline {version} — {comp.name} (manifest)",
             "--notes-file", str(notes_file)]
        )


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def cut(
    config: ReleaseConfig,
    version: str,
    *,
    checkouts: Mapping[str, Path],
    out_dir: Path,
    runner: r.Runner,
    respin: str | None = None,
    skip_tests: bool = False,
    skip_smoke: bool = False,
    report: r.Report = print,
) -> TrainPlan:
    m.validate_version(version)
    manifest_comp = config.manifest_component

    to_build = [config.component(respin)] if respin else list(config.components)
    # The manifest component's checkout is READ (carry-forward source) and
    # WRITTEN (the new train record) on every cut, respins included — so it is
    # preflighted on every cut, not only when it rebuilds (#207 review).
    preflight_comps = list(to_build)
    if manifest_comp.name not in {c.name for c in preflight_comps}:
        preflight_comps.append(manifest_comp)
    cutter = Cutter(
        config=config,
        runner=runner,
        checkouts=checkouts,
        out_dir=out_dir,
        report=report,
        skip_tests=skip_tests,
        skip_smoke=skip_smoke,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    states = cutter.preflight(preflight_comps)
    # Load the previous train only AFTER the manifest checkout passed
    # preflight — reading carry-forward entries from an unverified checkout is
    # how a stale manifest silently poisons a respin (#207 review).
    previous = m.load_manifest(checkouts[manifest_comp.name] / MANIFEST_RELPATH)
    plan = m.plan_train(
        config,
        version,
        heads={name: state.head or "" for name, state in states.items()},
        previous=previous,
        respin=respin,
    )
    cutter.sdk_version = m.sdk_train_version(config, plan)

    report(f"\ntrain {version}" + (f" (respin of {respin})" if respin else ""))
    for sp in plan.services:
        flag = "build" if sp.rebuilt else f"carry {sp.tag}"
        report(f"  {sp.service:<12} {sp.repo:<26} {sp.sha[:12]}  [{flag}]  {sp.wheel}")

    if runner.dry_run:
        report("\ndry run: nothing built, tagged, or published.")
        return plan

    for comp in to_build:
        report(f"\nbuild {comp.name}")
        cutter.run_tests(comp)
        cutter.build(comp, plan.for_component(comp.name)[0].sha, version)

    report("\nmanifest")
    cutter.write_manifest(plan)

    report("\nsmoke (spec §2.1)")
    for comp in to_build:
        cutter.smoke(comp, plan, version)

    report("\npublish")
    for comp in to_build:
        cutter.publish(comp, plan, version)
    if manifest_comp.name not in {c.name for c in to_build}:
        # A respin of another component still ends in a platform release —
        # the manifest carrier "latest" resolves against (spec §4; #207
        # review: without this the respun train is invisible to sync).
        cutter.publish_manifest_only(plan, version)

    # The checkout train record is written ONLY once everything published —
    # a failed cut must not arm plan_train's immutability check (#207 review).
    cutter.record_manifest(plan)

    report(f"\ncut {version} complete.")
    for note in cutter.notes:
        report(f"  next: {note}")
    return plan


def status(
    config: ReleaseConfig,
    *,
    checkouts: Mapping[str, Path],
    runner: r.Runner,
    report: r.Report = print,
) -> int:
    """What the current train is, and what a cut would pick up right now."""
    manifest_comp = config.manifest_component
    manifest = m.load_manifest(checkouts[manifest_comp.name] / MANIFEST_RELPATH)
    if manifest:
        report(f"train: {manifest.get('version')} ({MANIFEST_RELPATH})")
    else:
        report(f"train: none yet — no {MANIFEST_RELPATH} in {checkouts[manifest_comp.name]}")
    entries: Mapping[str, Mapping[str, object]] = (manifest or {}).get("components", {})

    report("")
    report(f"{'service':<12} {'tag':<9} {'train sha':<13} {'HEAD':<13} status")
    for comp in config.components:
        ignore = (str(MANIFEST_RELPATH),) if comp.carries_manifest else ()
        state = _state(runner, comp.name, checkouts[comp.name], ignore)
        issues = m.preflight_issues(state)
        head = (state.head or "-")[:12]
        for svc in comp.services:
            entry = entries.get(svc.name) or {}
            tag = str(entry.get("tag", "-"))
            sha = str(entry.get("sha", ""))[:12] or "-"
            if not state.exists:
                note = "checkout missing"
            elif issues:
                note = "; ".join(issues)
            elif sha == head:
                note = "up to date"
            else:
                note = "would be rebuilt at HEAD"
            report(f"{svc.name:<12} {tag:<9} {sha:<13} {head:<13} {note}")
    return 0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _state(runner: r.Runner, name: str, path: Path, ignore: tuple[str, ...]) -> m.CheckoutState:
    state = r.checkout_state(runner, name, path)
    if not state.dirty or not ignore:
        return state
    porcelain = runner.read(["git", "status", "--porcelain"], cwd=path)
    remaining = [
        line for line in porcelain.splitlines()
        if line.strip() and line[3:].strip() not in ignore
    ]
    from dataclasses import replace

    return replace(state, dirty=bool(remaining))


def _wheel_path(find_links: list[Path], filename: str) -> Path:
    """The exact wheel FILE for a smoke install — never resolved by name.

    Hard-fails on a missing asset: with PyPI still in the resolution path for
    the lock's third-party deps, a name-based install of a snowline package
    could mask a missing release asset (or resolve a name-squat) instead of
    failing the smoke test (#207 review)."""
    for d in find_links:
        candidate = d / filename
        if candidate.exists():
            return candidate
    raise ReleaseError(
        f"expected wheel {filename} not found in any asset dir "
        f"({', '.join(str(d) for d in find_links)}) — a missing release asset "
        "must fail smoke here, not be masked by an index fallback"
    )


def _stamp_targets(comp: Component, tree: Path) -> list[Path]:
    """Every pyproject whose package this component ships.

    Read off the config's per-service `pyproject` (falling back to the repo
    root) rather than guessed from the workspace members, so the set of things
    that get a train version is exactly the set of things that get published.
    """
    seen: list[Path] = []
    for svc in comp.services:
        target = tree / svc.pyproject
        if target not in seen:
            seen.append(target)
    return seen


class _throwaway_worktree:
    """A git worktree at `sha`, removed on the way out — build isolation.

    Building from here rather than the operator's checkout is what lets the
    cutter stamp versions into pyproject files without ever writing to a tracked
    file the operator cares about, and guarantees the wheels are built from the
    exact sha the tag will point at rather than whatever is in the working tree.
    """

    def __init__(self, runner: r.Runner, repo: Path, sha: str, report: r.Report):
        self.runner = runner
        self.repo = repo
        self.sha = sha
        self.report = report
        self.path: Path | None = None
        self._tmp: str | None = None

    def __enter__(self) -> Path:
        self._tmp = tempfile.mkdtemp(prefix="snowline-release-")
        self.path = Path(self._tmp) / "tree"
        self.runner.read(
            ["git", "worktree", "add", "--detach", str(self.path), self.sha], cwd=self.repo
        )
        self.report(f"  build tree: {self.path} @ {self.sha[:12]}")
        return self.path

    def __exit__(self, *exc) -> None:
        if self.path is not None:
            self.runner.read(
                ["git", "worktree", "remove", "--force", str(self.path)],
                cwd=self.repo,
                check=False,
            )
        if self._tmp and os.path.exists(self._tmp):
            shutil.rmtree(self._tmp, ignore_errors=True)
