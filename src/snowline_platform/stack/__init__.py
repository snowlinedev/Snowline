"""The packaged-spoke install/update machinery behind `snowline stack sync`
and `snowline stack bootstrap-spoke` (items b70b0359 / issue #203 and
71317cd6 / snowline-pm#122).

Three modules, the same split as `release/`:

- `model` — pure. Layout, the train-manifest reader, stack.json, the
  spoke-only guard, migration-crossed detection, symlink-swap planning + GC,
  env/plist rendering, the run report, the auto-upgrade failure-posture
  split; plus bootstrap-spoke's preconditions, seed-config builder, and its
  own (parallel) run report.
- `sync` — the `stack sync` orchestration, reusing `release.runner.Runner`
  (the SAME git/gh/uv/launchctl/createdb subprocess seam) plus the
  filesystem writes (symlink swap, env/plist/report files) `--dry-run` never
  reaches.
- `bootstrap` — the `stack bootstrap-spoke` orchestration: WRAPS `snowline
  replicate pair`/`seed`/`reseed-check` verbatim through the SAME Runner
  seam, never reimplementing replication logic.

Design record: `docs/specs/macos-distribution.md` §5, §6, §9; the
auto-upgrade requirements in the work item body (Sean, 2026-08-30).
"""

from __future__ import annotations

from .bootstrap import run_bootstrap_spoke
from .model import (
    BootstrapReport,
    RunReport,
    StackConfig,
    StackError,
    TrainManifest,
    bootstrap_exit_code,
    exit_code,
)
from .sync import DEFAULT_HEALTH_URL, run_sync, tty_prompt

__all__ = [
    "DEFAULT_HEALTH_URL",
    "BootstrapReport",
    "RunReport",
    "StackConfig",
    "StackError",
    "TrainManifest",
    "bootstrap_exit_code",
    "exit_code",
    "run_bootstrap_spoke",
    "run_sync",
    "tty_prompt",
]
