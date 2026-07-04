"""Memory's PLUGIN-SPECIFIC registration assertions — the manifest it posts (one
surface, mapped onto `main`, no isolated surface) and that its
`registration_heartbeat` wrapper wires into the shared SDK loop.

The heartbeat MECHANISM (retry, 409-as-success, non-finite interval guard,
first-beat INFO, the httpx log filter) is tested ONCE in the SDK
(`sdk/tests/test_registration.py`, issue #50). The ~30-line loop harness is
imported from `snowline_plugin_sdk.testing` rather than redefined here.

Stubs the platform HTTP with an `httpx.MockTransport`, so no platform runs.
"""

from __future__ import annotations

import json

import httpx
from snowline_plugin_sdk.testing import mock_client, run_heartbeat_until

from snowline_memory import registration


def test_register_posts_the_right_manifest():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"name": "memory", "status": "unknown"})

    ok = registration.register_with_platform(
        platform_url="http://platform.example",
        base_url="http://mem.example:8802",
        client=mock_client(handler),
    )
    assert ok is True
    assert captured["url"] == "http://platform.example/plugins"
    m = captured["json"]
    assert m["name"] == "memory"
    assert m["base_url"] == "http://mem.example:8802"
    assert m["mcp_path"] == "/mcp"
    assert m["health_path"] == "/health"
    # One surface, mapped onto the platform's `main` — no isolated surface.
    assert m["surfaces"] == {"/mcp": "main"}


def test_heartbeat_reasserts_memory_manifest_every_beat():
    # The memory wrapper wires into the shared SDK loop (issue #50): the loop
    # keeps re-POSTing (self-healing, issue #39). Uses the imported harness.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "memory", "outcome": "unchanged"})

    got = run_heartbeat_until(
        registration.registration_heartbeat,
        handler,
        beats=3,
        platform_url="http://platform.example",
        interval=0.01,
    )
    assert got >= 3


def test_manifest_is_accepted_by_the_platform_model():
    """The manifest memory posts validates against the platform's manifest model —
    the contract both sides share. This is the only place memory touches a
    platform symbol, and it's a TEST-only compatibility check, not a runtime
    import (import-purity)."""
    from snowline_platform.manifest import PluginManifest

    m = PluginManifest(**registration.build_manifest("http://mem.example:8802"))
    assert m.name == "memory"
    assert m.surfaces == {"/mcp": "main"}
