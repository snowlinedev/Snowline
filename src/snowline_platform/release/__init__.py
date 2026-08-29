"""The release train machinery behind `snowline release` (item a0ef1bd4 / #202).

Three modules, split along the seam that makes the interesting parts testable:

- `model` — pure. Config, the train plan (respin carry-forward, immutability),
  version stamping, the SDK-pin rewrite, preflight and tag decisions, release
  notes. No subprocess, no network.
- `runner` — the only place `git`/`gh`/`uv`/`npm`/`psql` are executed, with the
  read-vs-mutate distinction that gives `--dry-run` its meaning.
- `cutter` — the orchestration, plus `_smoke_boot`, the §2.1 driver that runs
  inside a throwaway venv built from the release wheels.

Design record lives in `docs/specs/macos-distribution.md` §2, §2.1 and §4.
"""

from __future__ import annotations

from .cutter import cut, status
from .model import (
    ReleaseConfig,
    ReleaseError,
    TrainPlan,
    load_config,
    plan_train,
    resolve_checkout,
)

__all__ = [
    "ReleaseConfig",
    "ReleaseError",
    "TrainPlan",
    "cut",
    "load_config",
    "plan_train",
    "resolve_checkout",
    "status",
]
