"""The packaged-spoke install/update machinery behind `snowline stack sync`
(item b70b0359 / issue #203).

Two modules, the same split as `release/`:

- `model` — pure. Layout, the train-manifest reader, stack.json, the
  spoke-only guard, migration-crossed detection, symlink-swap planning + GC,
  env/plist rendering, the run report, the auto-upgrade failure-posture
  split.
- `sync` — the orchestration, reusing `release.runner.Runner` (the SAME
  git/gh/uv/launchctl/createdb subprocess seam) plus the filesystem writes
  (symlink swap, env/plist/report files) `--dry-run` never reaches.

Design record: `docs/specs/macos-distribution.md` §5, §6, §9; the
auto-upgrade requirements in the work item body (Sean, 2026-08-30).
"""

from __future__ import annotations

from .model import RunReport, StackConfig, StackError, TrainManifest, exit_code
from .sync import DEFAULT_HEALTH_URL, run_sync

__all__ = [
    "DEFAULT_HEALTH_URL",
    "RunReport",
    "StackConfig",
    "StackError",
    "TrainManifest",
    "exit_code",
    "run_sync",
]
