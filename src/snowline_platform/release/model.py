"""The pure half of the release cutter: config, planning, and text surgery.

Everything here is a function of its arguments — no subprocesses, no network,
no clock. `cutter.py` owns the side effects and calls into this module for every
decision it makes, which is what lets the interesting rules (respin carry-
forward, the SDK-pin rewrite, version stamping, preflight refusals) be tested
without a checkout, a Postgres, or a `gh` token.

Two vocabularies meet here and they are NOT the same thing:

- a **component** is a *repo* — the unit that gets tagged and gets a GitHub
  release. There are two: `platform` (snowlinedev/Snowline) and `pm`
  (snowlinedev/snowline-pm).
- a **service** is a *wheel* — the unit `snowline stack sync` builds a venv
  for. The platform component publishes four (platform, governance, memory,
  and the SDK library); pm publishes one.

The macOS distribution spec §4 keys `train.json`'s `components` map by service
name, and that is what `TrainPlan.to_manifest()` emits — the map is per-service
because sync resolves per-service, while tagging is per-repo because a tag is a
property of a repo. `--respin` therefore names a *component*: respinning
`platform` re-tags one repo and moves all four of its services together, which
is the only coherent reading of "re-tag only that component" when three wheels
share a git history.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

SDK_PACKAGE = "snowline-plugin-sdk"

# v0.MINOR.PATCH (spec §4). The leading `v` is the tag form; PEP 440 wants it
# gone, hence `pep440`.
_VERSION_RE = re.compile(r"^v0\.(\d+)\.(\d+)$")


class ReleaseError(Exception):
    """Anything the cutter refuses to do, phrased for the operator."""


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------


def validate_version(version: str) -> str:
    if not _VERSION_RE.match(version):
        raise ReleaseError(
            f"version {version!r} is not the train scheme: v0.MINOR.PATCH "
            "(spec §4 — MINOR per train cut, PATCH for a respin)"
        )
    return version


def pep440(version: str) -> str:
    """`v0.1.0` -> `0.1.0`. Tags carry the `v`; wheels must not."""
    return version.removeprefix("v")


def wheel_filename(package: str, version: str) -> str:
    """The wheel `uv build` will emit for `package` at the train version.

    Every Snowline package is pure-Python and hatchling-built, so the tag
    triple is fixed. The cutter predicts the name here and then asserts the
    file exists after the build rather than globbing — a glob would happily
    pick up a stale wheel from a previous train sitting in the same dist dir.
    """
    return f"{package.replace('-', '_')}-{pep440(version)}-py3-none-any.whl"


def requirements_filename(service: str) -> str:
    """The per-service lock export asset name (spec §2)."""
    return f"requirements-{service}.txt"


def dashboard_tarball_name(version: str) -> str:
    """The dashboard dist asset (spec §2.2)."""
    return f"dashboard-dist-{version}.tar.gz"


# --------------------------------------------------------------------------
# Config (release/components.json)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BootSpec:
    """How the wheel-boot smoke test (§2.1) stands a service up.

    `factory` is preferred over `attr` for every service that has one: the
    module-level `app` singletons opt into health polling, registration
    heartbeats and replication loops, none of which belong in a smoke test that
    only wants to prove boot-migrate reaches head against an empty database.
    """

    module: str
    database_url_env: str
    factory: str | None = None
    attr: str | None = None
    kwargs: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.factory and not self.attr:
            raise ReleaseError(
                f"boot spec for {self.module} names neither `factory` nor `attr`"
            )


@dataclass(frozen=True)
class Service:
    name: str
    package: str
    # "service" gets a venv on the target and a smoke test; "library" (the SDK)
    # is a wheel other services install from the same release assets. The kind
    # is recorded in train.json so `snowline stack sync` can iterate services
    # without trying to build a venv for a library.
    kind: str = "service"
    boot: BootSpec | None = None
    # The pyproject that carries this package's version, relative to its repo
    # root — what the cutter stamps the train version into. Stated per service
    # so the set of stamped files is exactly the set of published packages,
    # never guessed from workspace membership (`ops/remote-front` is a member
    # and must NOT be stamped or shipped).
    pyproject: str = "pyproject.toml"

    @property
    def is_service(self) -> bool:
        return self.kind == "service"


@dataclass(frozen=True)
class Dashboard:
    dir: str
    build: tuple[str, ...]
    dist: str
    # `npm ci` before the build: the dist that ships must come from the
    # committed `package-lock.json`, not from whatever `node_modules` the
    # operator's checkout happens to be carrying. Same reasoning as the
    # per-service lock exports (§2) applied to the one non-Python artifact.
    install: tuple[str, ...] = ()


@dataclass(frozen=True)
class Component:
    name: str
    repo: str
    path: str
    build: str  # "workspace" (uv build --all-packages) | "package" (uv build)
    services: tuple[Service, ...]
    test: tuple[str, ...] = ()
    test_env: Mapping[str, str] = field(default_factory=dict)
    prune_wheels: tuple[str, ...] = ()
    dashboard: Dashboard | None = None
    carries_manifest: bool = False
    rewrite_sdk_pin: bool = False

    def service(self, name: str) -> Service:
        for svc in self.services:
            if svc.name == name:
                return svc
        raise ReleaseError(f"component {self.name!r} publishes no service {name!r}")


@dataclass(frozen=True)
class ReleaseConfig:
    components: tuple[Component, ...]
    source: Path | None = None

    def component(self, name: str) -> Component:
        for comp in self.components:
            if comp.name == name:
                return comp
        known = ", ".join(c.name for c in self.components)
        raise ReleaseError(f"unknown component {name!r} (known: {known})")

    def services(self) -> list[tuple[Component, Service]]:
        return [(comp, svc) for comp in self.components for svc in comp.services]

    def component_of_service(self, service: str) -> Component:
        for comp, svc in self.services():
            if svc.name == service:
                return comp
        raise ReleaseError(f"unknown service {service!r}")

    @property
    def manifest_component(self) -> Component:
        for comp in self.components:
            if comp.carries_manifest:
                return comp
        raise ReleaseError("no component is marked `carries_manifest`")


def parse_config(data: Mapping[str, object], source: Path | None = None) -> ReleaseConfig:
    raw_components = data.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise ReleaseError("release config has no `components` list")
    components: list[Component] = []
    for raw in raw_components:
        services = tuple(
            Service(
                name=s["name"],
                package=s["package"],
                kind=s.get("kind", "service"),
                boot=BootSpec(**s["boot"]) if s.get("boot") else None,
                pyproject=s.get("pyproject", "pyproject.toml"),
            )
            for s in raw["services"]
        )
        dashboard = None
        if raw.get("dashboard"):
            d = raw["dashboard"]
            dashboard = Dashboard(
                dir=d["dir"],
                build=tuple(d["build"]),
                dist=d.get("dist", "dist"),
                install=tuple(d.get("install", ())),
            )
        components.append(
            Component(
                name=raw["name"],
                repo=raw["repo"],
                path=raw["path"],
                build=raw.get("build", "package"),
                services=services,
                test=tuple(raw.get("test", ())),
                test_env=dict(raw.get("test_env", {})),
                prune_wheels=tuple(raw.get("prune_wheels", ())),
                dashboard=dashboard,
                carries_manifest=bool(raw.get("carries_manifest", False)),
                rewrite_sdk_pin=bool(raw.get("rewrite_sdk_pin", False)),
            )
        )
    names = [c.name for c in components]
    if len(set(names)) != len(names):
        raise ReleaseError(f"duplicate component names in release config: {names}")
    service_names = [s.name for c in components for s in c.services]
    if len(set(service_names)) != len(service_names):
        raise ReleaseError(f"duplicate service names in release config: {service_names}")
    return ReleaseConfig(components=tuple(components), source=source)


def load_config(path: Path) -> ReleaseConfig:
    if not path.exists():
        raise ReleaseError(
            f"release config not found at {path}. `snowline release` runs from a "
            "platform checkout (or pass --config): the cutter drives local "
            "checkouts, so it needs the repo it is cutting from."
        )
    return parse_config(json.loads(path.read_text()), source=path)


def resolve_checkout(config: ReleaseConfig, component: Component, overrides: Mapping[str, str]) -> Path:
    """Where `component`'s checkout lives.

    `path` in the config is relative to the *config file's repo root* (i.e. the
    platform checkout), which keeps the checked-in default (`..`-relative
    sibling directories) honest for the one operator this channel serves.
    `--checkout name=path` overrides it for anyone laid out differently.
    """
    if component.name in overrides:
        return Path(overrides[component.name]).expanduser().resolve()
    root = config.source.parent.parent if config.source else Path.cwd()
    return (root / component.path).resolve()


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckoutState:
    component: str
    path: Path
    exists: bool = False
    is_git: bool = False
    branch: str | None = None
    dirty: bool = False
    head: str | None = None
    origin_head: str | None = None
    head_pushed: bool = False


def preflight_issues(state: CheckoutState) -> tuple[str, ...]:
    """Hard refusals. A cut tags a sha and publishes wheels built from it; every
    condition here is one where the tag would not mean what it claims."""
    if not state.exists:
        return (f"checkout missing at {state.path}",)
    if not state.is_git:
        return (f"{state.path} is not a git checkout",)
    issues: list[str] = []
    if state.branch != "main":
        issues.append(
            f"on branch {state.branch or '(detached)'}, expected main "
            "(the train tags blessed main SHAs — spec §4)"
        )
    if state.dirty:
        issues.append("working tree is dirty; commit or stash before cutting")
    if not state.head_pushed:
        issues.append(
            f"HEAD {(state.head or '')[:12]} is not on origin/main — push first, "
            "or the tag points at a sha nobody else can fetch"
        )
    return tuple(issues)


def preflight_warnings(state: CheckoutState) -> tuple[str, ...]:
    """Worth saying out loud, not worth refusing over."""
    if state.head and state.origin_head and state.head != state.origin_head:
        return (
            (
                f"HEAD {state.head[:12]} is behind origin/main "
                f"{state.origin_head[:12]}; the train will bless HEAD, not the "
                "remote tip"
            ),
        )
    return ()


# --------------------------------------------------------------------------
# The train manifest (release/train.json, spec §4)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ServicePlan:
    service: str
    component: str
    repo: str
    tag: str
    sha: str
    wheel: str
    kind: str
    # False = carried forward verbatim from the previous train. A respin's
    # whole point: only the respun component is rebuilt and re-tagged.
    rebuilt: bool = True


@dataclass(frozen=True)
class TrainPlan:
    version: str
    services: tuple[ServicePlan, ...]
    respin: str | None = None

    @property
    def components_to_build(self) -> tuple[str, ...]:
        seen: list[str] = []
        for plan in self.services:
            if plan.rebuilt and plan.component not in seen:
                seen.append(plan.component)
        return tuple(seen)

    def for_component(self, name: str) -> tuple[ServicePlan, ...]:
        return tuple(p for p in self.services if p.component == name)

    def to_manifest(self) -> dict:
        return {
            "version": self.version,
            "components": {
                p.service: {
                    "repo": p.repo,
                    "tag": p.tag,
                    "sha": p.sha,
                    "wheel": p.wheel,
                    "kind": p.kind,
                }
                for p in self.services
            },
        }


def plan_train(
    config: ReleaseConfig,
    version: str,
    *,
    heads: Mapping[str, str],
    previous: Mapping[str, object] | None = None,
    respin: str | None = None,
) -> TrainPlan:
    """Build the train plan — the single place respin semantics live.

    A MINOR cut moves every component to `version`. A `--respin <component>`
    re-tags and rebuilds only that component; every other service keeps the
    previous manifest's entry **verbatim**, tag included, because sync
    downloads each component from its manifest-recorded tag rather than
    assuming a uniform tag exists on every repo (spec §4).

    Re-running the same version is deliberately allowed — that is the recovery
    path after a partial failure (a tag pushed but the release not created).
    What is refused is re-cutting a version at a *different* sha, which would
    silently redefine an already-published train.
    """
    validate_version(version)
    previous_entries: dict[str, Mapping[str, object]] = {}
    previous_version: str | None = None
    if previous:
        previous_version = str(previous.get("version") or "") or None
        raw = previous.get("components") or {}
        if isinstance(raw, Mapping):
            previous_entries = {str(k): v for k, v in raw.items()}

    if respin is not None:
        respun = config.component(respin)  # raises on an unknown name
        if not previous_entries:
            raise ReleaseError(
                f"--respin {respin} needs an existing release/train.json to carry "
                "the other components forward from; cut a MINOR train first"
            )
        rebuilt_components = {respun.name}
    else:
        rebuilt_components = {c.name for c in config.components}

    plans: list[ServicePlan] = []
    for comp, svc in config.services():
        if comp.name in rebuilt_components:
            head = heads.get(comp.name)
            if not head:
                raise ReleaseError(f"no HEAD sha resolved for component {comp.name!r}")
            plans.append(
                ServicePlan(
                    service=svc.name,
                    component=comp.name,
                    repo=comp.repo,
                    tag=version,
                    sha=head,
                    wheel=wheel_filename(svc.package, version),
                    kind=svc.kind,
                    rebuilt=True,
                )
            )
            continue
        entry = previous_entries.get(svc.name)
        if not isinstance(entry, Mapping):
            raise ReleaseError(
                f"--respin {respin} carries {svc.name} forward, but the previous "
                "train manifest has no entry for it; cut a MINOR train instead"
            )
        plans.append(
            ServicePlan(
                service=svc.name,
                component=comp.name,
                repo=str(entry.get("repo", comp.repo)),
                tag=str(entry["tag"]),
                sha=str(entry["sha"]),
                wheel=str(entry["wheel"]),
                kind=str(entry.get("kind", svc.kind)),
                rebuilt=False,
            )
        )

    if previous_version == version:
        for plan in plans:
            entry = previous_entries.get(plan.service)
            if not isinstance(entry, Mapping):
                continue
            if entry.get("tag") == version and entry.get("sha") != plan.sha:
                raise ReleaseError(
                    f"train {version} was already recorded with {plan.service} at "
                    f"{str(entry.get('sha'))[:12]}, but the checkout is now at "
                    f"{plan.sha[:12]}. A published train is immutable — cut a new "
                    "PATCH (--respin) instead of redefining this one"
                )

    return TrainPlan(version=version, services=tuple(plans), respin=respin)


def load_manifest(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def render_manifest(plan: TrainPlan) -> str:
    return json.dumps(plan.to_manifest(), indent=2, sort_keys=False) + "\n"


# --------------------------------------------------------------------------
# Version stamping
# --------------------------------------------------------------------------

_PROJECT_TABLE_RE = re.compile(r"^\[project\]\s*$", re.MULTILINE)
_TABLE_RE = re.compile(r"^\[", re.MULTILINE)
_VERSION_LINE_RE = re.compile(r'^version\s*=\s*".*"\s*$', re.MULTILINE)


def stamp_pyproject_version(text: str, version: str) -> str:
    """Rewrite the `[project]` table's `version = "..."` to the train version.

    Deliberately narrow: it edits the `version` line inside `[project]` and
    nothing else, so a `[tool.*]` table that happens to carry a `version` key
    is untouched. The pyprojects all say `0.0.1`; the train version is what the
    wheels must carry, and the repos must not accumulate version churn for it —
    see `cut.py` for where this is applied (a throwaway git worktree, never the
    operator's checkout).
    """
    start = _PROJECT_TABLE_RE.search(text)
    if not start:
        raise ReleaseError("pyproject.toml has no [project] table")
    body_start = start.end()
    next_table = _TABLE_RE.search(text, body_start)
    body_end = next_table.start() if next_table else len(text)
    body = text[body_start:body_end]
    replaced, count = _VERSION_LINE_RE.subn(f'version = "{pep440(version)}"', body, count=1)
    if count != 1:
        raise ReleaseError("pyproject.toml [project] table has no `version = \"...\"` line")
    return text[:body_start] + replaced + text[body_end:]


# --------------------------------------------------------------------------
# The SDK-pin rewrite (spec risk #6)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SdkPinRewrite:
    text: str
    found: bool
    previous: str | None = None
    git_rev: str | None = None

    @property
    def was_git_pin(self) -> bool:
        return self.git_rev is not None


_REQ_NAME_RE = re.compile(r"^([A-Za-z0-9._-]+)")
_GIT_REV_RE = re.compile(r"git\+[^\s#]*@([0-9a-fA-F]{7,40})")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _blocks(text: str) -> list[list[str]]:
    """Split a `uv export` into logical requirement blocks.

    A block starts at a column-0 non-comment line and swallows every following
    indented line — hash continuations (`    --hash=...`) and the `    # via`
    provenance comments alike.
    """
    out: list[list[str]] = []
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and out:
            out[-1].append(line)
        else:
            out.append([line])
    return out


def rewrite_sdk_pin(text: str, version: str, *, package: str = SDK_PACKAGE) -> SdkPinRewrite:
    """Repoint pm's SDK requirement at the train's SDK **wheel** (risk #6).

    pm consumes the SDK as a git dependency pinned to a rev, so a naive
    `uv export` emits

        snowline-plugin-sdk @ git+https://github.com/snowlinedev/Snowline.git@<rev>#subdirectory=sdk

    — which would drag a source clone (and a git round-trip, and the private-
    repo/auth question) back into an install that is supposed to be
    not-from-source. Rewriting it to `snowline-plugin-sdk==<train version>`
    resolves it against the SDK wheel already sitting in the platform release's
    assets, so a pm venv builds from release assets alone, offline from git —
    the acceptance the spec states.

    Hash continuations are dropped with the git line (a `==` pin resolved from
    `--find-links` has no recorded hash to keep, and a stale one would make the
    install fail closed for the wrong reason). Extras are not preserved because
    `uv export` has already flattened them into the exported dependency set.
    """
    target = _normalize(package)
    rebuilt: list[str] = []
    found = False
    previous: str | None = None
    git_rev: str | None = None
    for block in _blocks(text):
        head = block[0]
        match = _REQ_NAME_RE.match(head)
        if not match or _normalize(match.group(1)) != target:
            rebuilt.extend(block)
            continue
        found = True
        previous = head
        rev = _GIT_REV_RE.search(head)
        git_rev = rev.group(1) if rev else None
        marker = ""
        if ";" in head:
            marker = " ;" + head.split(";", 1)[1].rstrip(" \\")
        rebuilt.append(f"{package}=={pep440(version)}{marker}")
        # Keep the provenance comments, drop hash continuations.
        rebuilt.extend(
            line for line in block[1:] if line.lstrip().startswith("#")
        )
    out = "\n".join(rebuilt)
    if text.endswith("\n"):
        out += "\n"
    return SdkPinRewrite(text=out, found=found, previous=previous, git_rev=git_rev)


# --------------------------------------------------------------------------
# Release notes
# --------------------------------------------------------------------------


def format_release_notes(
    *,
    component: str,
    repo: str,
    version: str,
    previous_tag: str | None,
    subjects: Sequence[str],
    assets: Iterable[str] = (),
) -> str:
    """Notes from merged PRs since the component's previous tag.

    Both repos squash-merge with the PR number in the subject
    (`Some change (#123)`), so `git log --first-parent <prev>..<sha>` already
    *is* the merged-PR list. Deliberately not calling the GitHub API for this:
    the git history on the sha being tagged is the honest answer, and it works
    the same for the private repo.
    """
    lines = [f"Snowline train `{version}` — **{component}** (`{repo}`)", ""]
    if previous_tag:
        lines.append(f"Changes since `{previous_tag}`:")
    else:
        lines.append("First train tag on this repo — recent history:")
    lines.append("")
    if subjects:
        lines.extend(f"- {s}" for s in subjects)
    else:
        lines.append("_No commits since the previous tag — respun for the train._")
    asset_list = list(assets)
    if asset_list:
        lines += ["", "Assets:", ""]
        lines.extend(f"- `{a}`" for a in asset_list)
    lines += [
        "",
        (
            "Installed by `snowline stack sync` from the train manifest in the "
            f"`{version}` release of `snowlinedev/Snowline` "
            "(macOS distribution spec §4–§5)."
        ),
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Idempotency decisions
# --------------------------------------------------------------------------


def tag_decision(*, tag: str, target_sha: str, existing_sha: str | None) -> str:
    """`create` | `skip` | refuse.

    Re-runnability after a partial failure is the whole point: an already-
    pushed tag at the right sha is a no-op, not an error. A tag at a *different*
    sha is a refusal — moving a published tag rewrites what a train means for
    anyone who already synced it.
    """
    if existing_sha is None:
        return "create"
    if existing_sha == target_sha:
        return "skip"
    raise ReleaseError(
        f"tag {tag} already exists at {existing_sha[:12]} but this cut wants "
        f"{target_sha[:12]}. Refusing to move a published tag — cut a PATCH "
        "(--respin) instead."
    )
