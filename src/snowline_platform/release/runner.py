"""The process seam: every `git`, `gh`, `uv`, `npm` and `psql` call goes here.

One class, one distinction that carries the whole `--dry-run` contract:

- `read()` always executes. Reads are safe, and a dry run that cannot read the
  checkouts cannot report what a real cut would do.
- `run()` mutates. Under `--dry-run` it prints the command and returns "" —
  nothing is tagged, uploaded, built or dropped.

Tests inject a `FakeRunner` with the same two methods, which is why nothing in
`cutter.py` ever reaches for `subprocess` directly.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .model import CheckoutState, ReleaseError

Report = Callable[[str], None]


class CommandError(ReleaseError):
    def __init__(self, argv: Sequence[str], returncode: int, output: str):
        self.argv = list(argv)
        self.returncode = returncode
        self.output = output
        super().__init__(
            f"command failed ({returncode}): {shlex.join(argv)}\n{output.strip()}"
        )


@dataclass
class Runner:
    report: Report = print
    dry_run: bool = False
    verbose: bool = False
    executed: list[list[str]] = field(default_factory=list)

    def _exec(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> str:
        if self.verbose:
            self.report(f"  $ {shlex.join(argv)}" + (f"  (in {cwd})" if cwd else ""))
        merged = dict(os.environ)
        merged.update(env or {})
        # argv lists, never a shell string. `check=False` because the
        # returncode is inspected below — `check` here is the CALLER's contract.
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd else None,
            env=merged,
            capture_output=True,
            text=True,
            check=False,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if check and proc.returncode != 0:
            raise CommandError(argv, proc.returncode, out)
        self.executed.append(list(argv))
        return proc.stdout or ""

    def read(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> str:
        """A non-mutating command. Runs even under --dry-run."""
        return self._exec(argv, cwd=cwd, env=env, check=check)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> str:
        """A mutating command. Skipped (and printed) under --dry-run."""
        if self.dry_run:
            self.report(f"  would run: {shlex.join(argv)}" + (f"  (in {cwd})" if cwd else ""))
            return ""
        return self._exec(argv, cwd=cwd, env=env, check=check)


# --------------------------------------------------------------------------
# git / gh queries, all reads
# --------------------------------------------------------------------------


def checkout_state(runner: Runner, component: str, path: Path) -> CheckoutState:
    if not path.exists():
        return CheckoutState(component=component, path=path, exists=False)
    inside = runner.read(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=path, check=False
    ).strip()
    if inside != "true":
        return CheckoutState(component=component, path=path, exists=True, is_git=False)
    branch = runner.read(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path).strip()
    dirty = bool(runner.read(["git", "status", "--porcelain"], cwd=path).strip())
    head = runner.read(["git", "rev-parse", "HEAD"], cwd=path).strip()
    # Refresh remote refs so "is it pushed" is a fact, not a stale cache.
    runner.read(["git", "fetch", "--quiet", "origin", "main"], cwd=path, check=False)
    origin_head = runner.read(
        ["git", "rev-parse", "origin/main"], cwd=path, check=False
    ).strip() or None
    # "Pushed" means origin/main contains HEAD — equal to the tip, or an
    # ancestor of it (someone landed more after the sha we're blessing).
    pushed = bool(origin_head) and (
        head == origin_head or _ancestor(runner, path, head, origin_head)
    )
    return CheckoutState(
        component=component,
        path=path,
        exists=True,
        is_git=True,
        branch=branch,
        dirty=dirty,
        head=head,
        origin_head=origin_head,
        head_pushed=pushed,
    )


def _ancestor(runner: Runner, path: Path, sha: str, ancestor_of: str) -> bool:
    try:
        runner.read(
            ["git", "merge-base", "--is-ancestor", sha, ancestor_of], cwd=path
        )
        return True
    except CommandError:
        return False


def existing_tag_sha(runner: Runner, path: Path, tag: str) -> str | None:
    """The sha `tag` points at on **origin** — the only copy that matters.

    A local-only tag is not published; `git ls-remote` is the source of truth
    for "has this train already been tagged".
    """
    out = runner.read(
        ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"], cwd=path, check=False
    ).strip()
    if not out:
        return None
    sha = out.split()[0]
    # Annotated tags: dereference to the commit the tag object wraps.
    peeled = runner.read(
        ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}^{{}}"],
        cwd=path,
        check=False,
    ).strip()
    if peeled:
        sha = peeled.split()[0]
    return sha


def previous_tag(runner: Runner, path: Path, before: str) -> str | None:
    """The most recent `v0.*` tag reachable from `before`, or None."""
    out = runner.read(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v0.*", f"{before}^"],
        cwd=path,
        check=False,
    ).strip()
    return out or None


def commit_subjects(runner: Runner, path: Path, since: str | None, until: str, limit: int = 50) -> list[str]:
    rng = f"{since}..{until}" if since else until
    out = runner.read(
        ["git", "log", "--first-parent", "--pretty=%s", f"-{limit}", rng],
        cwd=path,
        check=False,
    )
    return [line for line in out.splitlines() if line.strip()]


def release_exists(runner: Runner, repo: str, tag: str) -> bool:
    try:
        runner.read(["gh", "release", "view", tag, "--repo", repo, "--json", "tagName"])
        return True
    except CommandError:
        return False


def gh_auth_ok(runner: Runner) -> bool:
    try:
        runner.read(["gh", "auth", "status"])
        return True
    except CommandError:
        return False
