"""Which release TRAIN this process is running (issue #259, the standby
readiness surface).

Train wheels are versioned per train (`snowline_platform-0.5.1-…whl` is train
`v0.5.1`, release/model.py `pep440`), so an installed service knows its train
from its own distribution metadata. A service run from an EDITABLE checkout
(the hub runs from source) carries the checkout's placeholder version
(`0.0.1`), which names no train — it reports `version: "source"` and the
checkout's git sha instead, so "which code is this?" stays answerable without
pretending to be a train.

Shape (what a service puts on its `/health` body as `train`, and the platform
on its replication self-manifest as `train_version` = `version`):

    {"version": "v0.5.1" | "source" | None,
     "package": "snowline-governance", "package_version": "0.5.1" | None,
     "editable": bool, "git_sha": "<40 hex>" | None}

Never raises: an unresolvable distribution reports `version: None`.
Stdlib only (the SDK base install has no third-party dependency).
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
from importlib import metadata
from pathlib import Path

SOURCE = "source"

_TRAIN_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_train(version: str | None) -> tuple[int, int, int] | None:
    """`v0.5.1` / `0.5.1` → (0, 5, 1); anything else (``source``, a pre-release,
    None) → None. Train tags are plain three-part semver (release/model.py
    `validate_version`)."""
    if not version:
        return None
    m = _TRAIN_RE.match(version.strip())
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def _direct_url(dist: metadata.Distribution) -> dict | None:
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:  # noqa: BLE001 - unreadable metadata is "no direct_url"
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _editable_dir(direct: dict | None) -> Path | None:
    if not direct or not (direct.get("dir_info") or {}).get("editable"):
        return None
    url = str(direct.get("url") or "")
    if url.startswith("file://"):
        return Path(url[len("file://"):])
    return None


def git_sha(path: Path) -> str | None:
    """HEAD of the git checkout containing `path`, or None (not a checkout, no
    git, or it took too long — a health body must stay cheap)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def running_train(package: str) -> dict:
    """The running train of installed distribution `package` (shape in the
    module docstring). Uncached — see `cached_running_train`."""
    info: dict = {
        "version": None,
        "package": package,
        "package_version": None,
        "editable": False,
        "git_sha": None,
    }
    try:
        dist = metadata.distribution(package)
    except metadata.PackageNotFoundError:
        return info
    info["package_version"] = dist.version
    editable = _editable_dir(_direct_url(dist))
    if editable is not None:
        info["editable"] = True
        info["version"] = SOURCE
        info["git_sha"] = git_sha(editable)
        return info
    parsed = parse_train(dist.version)
    info["version"] = f"v{parsed[0]}.{parsed[1]}.{parsed[2]}" if parsed else None
    return info


@functools.cache
def _cached(package: str) -> str:
    return json.dumps(running_train(package))


def cached_running_train(package: str) -> dict:
    """`running_train`, resolved once per process: the installed code cannot
    change under a running process (a train swap restarts it), so `/health`
    pays the metadata read + git call only on its first answer."""
    return json.loads(_cached(package))
