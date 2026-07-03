"""Plugin registration — posts the right manifest, and is best-effort/robust.

Stubs the platform HTTP with an `httpx.MockTransport`, so no platform runs. No
DB needed. Mirrors governance's registration tests.
"""

from __future__ import annotations

import json
import logging

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


def test_heartbeat_interval_env_is_lenient(monkeypatch):
    # A malformed or hot-looping value in the SHARED env var must not kill the
    # heartbeat (a dead heartbeat = a hollow gateway after the next platform
    # restart) — warn and fall back instead.
    from snowline_memory import config

    monkeypatch.delenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", raising=False)
    assert config.registration_heartbeat_seconds() == 15.0
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "15s")
    assert config.registration_heartbeat_seconds() == 15.0  # malformed → default
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "0")
    assert config.registration_heartbeat_seconds() == 1.0  # floored, no hot loop
    # "inf"/"nan"/"infinity" all parse as floats and slip past the bare `< 1.0`
    # floor, but anyio.sleep(inf/nan) never returns — a silently dead heartbeat,
    # the exact failure this lenient parse exists to prevent.
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "inf")
    assert config.registration_heartbeat_seconds() == 15.0  # non-finite → default
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "infinity")
    assert config.registration_heartbeat_seconds() == 15.0  # non-finite → default
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "nan")
    assert config.registration_heartbeat_seconds() == 15.0  # non-finite → default
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "30")
    assert config.registration_heartbeat_seconds() == 30.0


def test_heartbeat_survives_client_construction_failure(monkeypatch):
    # httpx.Client(...) is constructed lazily INSIDE the guarded loop (first
    # beat) precisely so a construction failure (e.g. a broken SSL_CERT_FILE)
    # can't escape and cancel the lifespan task group. Simulate that: the
    # first construction attempt raises, the second succeeds — subsequent
    # beats must still fire.
    construct_calls = {"n": 0}
    beat_count = {"n": 0}
    real_client_cls = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        beat_count["n"] += 1
        return httpx.Response(200, json={})

    def flaky_client(*args, **kwargs):
        construct_calls["n"] += 1
        if construct_calls["n"] == 1:
            raise RuntimeError("broken SSL_CERT_FILE")
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(registration.httpx, "Client", flaky_client)

    async def main():
        async with anyio.create_task_group() as tg:

            async def _beat():
                await registration.registration_heartbeat(
                    "http://platform.example", interval=0.01
                )

            tg.start_soon(_beat)
            with anyio.fail_after(5):
                while beat_count["n"] < 3:
                    await anyio.sleep(0.005)
            tg.cancel_scope.cancel()

    anyio.run(main)
    assert construct_calls["n"] == 2  # first raised, second succeeded
    assert beat_count["n"] >= 3


def test_heartbeat_confirms_first_beat_exactly_once(caplog):
    # One guaranteed INFO "registration confirmed" line on the first
    # successful beat (200 or 201), so a restart against an already-up
    # platform (200, not 201) is distinguishable from a heartbeat that never
    # started. Steady-state beats stay DEBUG — this must fire exactly once
    # even over several beats.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "memory"})

    with caplog.at_level(logging.INFO, logger="snowline_memory.registration"):
        _run_heartbeat_until(handler, beats=3)

    confirmations = [
        r for r in caplog.records if "registration confirmed" in r.getMessage()
    ]
    assert len(confirmations) == 1


def test_httpx_filter_drops_only_heartbeat_post_plugins_lines():
    # The scoped httpx log filter (replacing the process-wide WARNING cap):
    # it must drop ONLY the heartbeat's `POST .../plugins` request lines,
    # letting every other httpx request trace through.
    from snowline_memory.app import _HeartbeatHttpxLogFilter

    log_filter = _HeartbeatHttpxLogFilter()

    def _record(msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )

    heartbeat_line = _record(
        'HTTP Request: POST http://platform.example/plugins "HTTP/1.1 200 OK"'
    )
    other_get_line = _record(
        'HTTP Request: GET http://platform.example/scopes/foo "HTTP/1.1 200 OK"'
    )
    other_post_line = _record(
        'HTTP Request: POST http://example.com/webhook "HTTP/1.1 200 OK"'
    )

    assert log_filter.filter(heartbeat_line) is False
    assert log_filter.filter(other_get_line) is True
    assert log_filter.filter(other_post_line) is True


def test_manifest_is_accepted_by_the_platform_model():
    """The manifest memory posts validates against the platform's manifest model —
    the contract both sides share. This is the only place memory touches a
    platform symbol, and it's a TEST-only compatibility check, not a runtime
    import (import-purity)."""
    from snowline_platform.manifest import PluginManifest

    m = PluginManifest(**registration.build_manifest("http://mem.example:8802"))
    assert m.name == "memory"
    assert m.surfaces == {"/mcp": "main"}
