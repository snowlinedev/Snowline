"""Plugin registration — posts the right manifest, and is best-effort/robust.

Stubs the platform HTTP with an `httpx.MockTransport`, so no platform runs. No
DB needed. Mirrors governance's registration tests.
"""

from __future__ import annotations

import json

import anyio
import httpx

from snowline_memory import registration


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _run_heartbeat_until(handler, *, beats: int) -> int:
    """Run the heartbeat loop (tiny interval, stubbed platform) until `beats`
    POSTs have landed, then cancel it — how the app lifespan tears it down."""
    count = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return handler(request)

    async def main():
        async with anyio.create_task_group() as tg:

            async def _beat():
                await registration.registration_heartbeat(
                    "http://platform.example",
                    interval=0.01,
                    client=_client(counting_handler),
                )

            tg.start_soon(_beat)
            with anyio.fail_after(5):
                while count < beats:
                    await anyio.sleep(0.005)
            tg.cancel_scope.cancel()

    anyio.run(main)
    return count


def test_register_posts_the_right_manifest():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"name": "memory", "status": "unknown"})

    ok = registration.register_with_platform(
        platform_url="http://platform.example",
        base_url="http://mem.example:8802",
        client=_client(handler),
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


def test_register_idempotent_on_conflict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "already registered"})

    ok = registration.register_with_platform(
        platform_url="http://platform.example", client=_client(handler)
    )
    assert ok is True


def test_register_best_effort_when_platform_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("platform is down")

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


def test_heartbeat_reasserts_registration_every_beat():
    # The self-healing property (issue #39): the loop keeps re-POSTing, so a
    # platform whose in-memory registry was wiped gets the manifest again on
    # the next beat.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "memory", "outcome": "unchanged"})

    assert _run_heartbeat_until(handler, beats=3) >= 3


def test_heartbeat_outlives_failed_beats():
    # A down platform (transport error) and a server error must not kill the
    # loop — the beat after a failure still fires.
    responses = iter(
        [
            httpx.Response(201, json={}),
            "raise",
            httpx.Response(500, json={}),
            httpx.Response(200, json={}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        step = next(responses, None) or httpx.Response(200, json={})
        if step == "raise":
            raise httpx.ConnectError("platform is down")
        return step

    assert _run_heartbeat_until(handler, beats=4) >= 4


def test_manifest_is_accepted_by_the_platform_model():
    """The manifest memory posts validates against the platform's manifest model —
    the contract both sides share. This is the only place memory touches a
    platform symbol, and it's a TEST-only compatibility check, not a runtime
    import (import-purity)."""
    from snowline_platform.manifest import PluginManifest

    m = PluginManifest(**registration.build_manifest("http://mem.example:8802"))
    assert m.name == "memory"
    assert m.surfaces == {"/mcp": "main"}
