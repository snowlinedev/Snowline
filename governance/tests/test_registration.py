"""Plugin registration — posts the right manifest, and is best-effort/robust.

Stubs the platform HTTP with an `httpx.MockTransport`, so no platform runs. No
DB needed.
"""

from __future__ import annotations

import httpx

from snowline_governance import registration


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_register_posts_the_right_manifest():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(201, json={"name": "governance", "status": "unknown"})

    ok = registration.register_with_platform(
        platform_url="http://platform.example",
        base_url="http://gov.example:8801",
        client=_client(handler),
    )
    assert ok is True
    assert captured["url"] == "http://platform.example/plugins"
    m = captured["json"]
    assert m["name"] == "governance"
    assert m["base_url"] == "http://gov.example:8801"
    assert m["mcp_path"] == "/mcp"
    assert m["health_path"] == "/health"
    # The surface mapping: /mcp -> the platform's `main` named surface.
    assert m["surfaces"] == {"/mcp": "main", "/shadow/mcp": "shadow"}


def test_register_idempotent_on_conflict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "already registered"})

    ok = registration.register_with_platform(
        platform_url="http://platform.example", client=_client(handler)
    )
    assert ok is True  # already-registered is treated as success


def test_register_best_effort_when_platform_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("platform is down")

    # Must NOT raise — a down platform can't crash the plugin.
    ok = registration.register_with_platform(
        platform_url="http://platform.example", client=_client(handler)
    )
    assert ok is False


def test_register_returns_false_on_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    ok = registration.register_with_platform(
        platform_url="http://platform.example", client=_client(handler)
    )
    assert ok is False


def test_manifest_is_accepted_by_the_platform_model():
    """The manifest governance posts validates against the platform's manifest
    model — the contract both sides share (the platform accepts the surfaces
    mapping). This is the only place governance touches a platform symbol, and
    it's a TEST-only check of contract compatibility, not a runtime import."""
    from snowline_platform.manifest import PluginManifest

    m = PluginManifest(**registration.build_manifest("http://gov.example:8801"))
    assert m.name == "governance"
    assert m.surfaces == {"/mcp": "main", "/shadow/mcp": "shadow"}
