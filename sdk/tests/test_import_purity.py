"""Import-purity: importing the SDK package ROOT (`snowline_plugin_sdk`) must NOT
pull `httpx` (issue #50). The registration heartbeat + its test harness ride the
`[client]` extra and are imported EXPLICITLY (`snowline_plugin_sdk.registration`
/ `.testing`); the base install (and `import snowline_plugin_sdk`) stays
dependency-free so plugins that only need `verify_event` / the contract + UI
constants don't grow an httpx dependency.

Run in a SUBPROCESS: the SDK's own `test_registration.py` imports httpx into this
process, so an in-process `sys.modules` check would be meaningless.
"""

from __future__ import annotations

import subprocess
import sys


def test_package_root_does_not_import_httpx():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import snowline_plugin_sdk, sys; "
            "assert 'httpx' not in sys.modules, 'httpx leaked into base import'; "
            "assert 'anyio' not in sys.modules, 'anyio leaked into base import'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
