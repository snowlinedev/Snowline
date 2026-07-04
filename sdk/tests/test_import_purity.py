"""Import-purity: importing the SDK package ROOT (`snowline_plugin_sdk`) must NOT
pull `httpx` (issue #50) — nor `sqlalchemy`/`fastapi` (issue #77). The
registration heartbeat + its test harness ride the `[client]` extra and the
replication modules ride `[replication]`; both are imported EXPLICITLY
(`snowline_plugin_sdk.registration` / `.testing` / `.replication`); the base
install (and `import snowline_plugin_sdk`) stays dependency-free so plugins
that only need `verify_event` / the contract + UI constants grow no deps.

Run in a SUBPROCESS: the SDK's own `test_registration.py` / replication tests
import httpx/sqlalchemy into this process, so an in-process `sys.modules` check
would be meaningless.
"""

from __future__ import annotations

import subprocess
import sys


def test_package_root_does_not_import_optional_deps():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import snowline_plugin_sdk, sys; "
            "assert 'httpx' not in sys.modules, 'httpx leaked into base import'; "
            "assert 'anyio' not in sys.modules, 'anyio leaked into base import'; "
            "assert 'sqlalchemy' not in sys.modules, 'sqlalchemy leaked into base import'; "
            "assert 'fastapi' not in sys.modules, 'fastapi leaked into base import'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_replication_package_does_not_import_fastapi():
    """`replication`'s package root pulls sqlalchemy (its extra) but must NOT
    pull fastapi — `admin` is imported explicitly, so emit/ingest stay usable
    for a plugin wiring its own transport."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import snowline_plugin_sdk.replication, sys; "
            "assert 'fastapi' not in sys.modules, 'fastapi leaked into replication import'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
