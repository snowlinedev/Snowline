"""The shared registration heartbeat (issue #50) — the behavioral matrix that
used to be copy-pasted into governance's and memory's `test_registration.py`.

Stubs the platform HTTP with an `httpx.MockTransport`, so no platform runs. The
plugins keep only their plugin-specific manifest/surface assertions and import
`run_heartbeat_until` from here.
"""

from __future__ import annotations

import logging

import anyio
import httpx

from snowline_plugin_sdk import registration
from snowline_plugin_sdk.testing import mock_client, run_heartbeat_until

LOG = logging.getLogger("snowline_plugin_sdk.registration")
MANIFEST = {
    "name": "test",
    "base_url": "http://plugin.example:8899",
    "mcp_path": "/mcp",
    "health_path": "/health",
    "surfaces": {"/mcp": "main"},
}


def _register(handler, **kwargs) -> bool:
    return registration.register_with_platform(
        MANIFEST,
        "http://platform.example",
        plugin_name="test",
        log=LOG,
        client=mock_client(handler),
        **kwargs,
    )


def _heartbeat_kwargs(**overrides):
    kwargs = dict(
        manifest_builder=lambda: MANIFEST,
        platform_url="http://platform.example",
        plugin_name="test",
        log=LOG,
        interval=0.01,
    )
    kwargs.update(overrides)
    return kwargs


# --- register_with_platform -------------------------------------------------


def test_register_posts_to_the_plugins_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(201, json={"name": "test", "status": "unknown"})

    assert _register(handler) is True
    assert captured["url"] == "http://platform.example/plugins"
    assert captured["json"] == MANIFEST


def test_register_idempotent_on_conflict():
    # A legacy (pre-upsert) platform returns 409 — treated as success.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "already registered"})

    assert _register(handler) is True


def test_register_best_effort_when_platform_down():
    # Must NOT raise — a down platform can't crash the plugin.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("platform is down")

    assert _register(handler) is False


def test_register_returns_false_on_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    assert _register(handler) is False


def test_register_never_raises_on_malformed_platform_url():
    # httpx.InvalidURL is NOT an httpx.HTTPError subclass — a fat-fingered
    # SNOWLINE_PLATFORM_URL must still honor the "never raises" contract.
    assert (
        registration.register_with_platform(
            MANIFEST,
            "http://user:@:bad:url",
            plugin_name="test",
            log=LOG,
        )
        is False
    )


# --- registration_heartbeat -------------------------------------------------


def test_heartbeat_reasserts_registration_every_beat():
    # The self-healing property (issue #39): the loop keeps re-POSTing, so a
    # platform whose in-memory registry was wiped gets the manifest again on
    # the next beat.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"outcome": "unchanged"})

    got = run_heartbeat_until(
        registration.registration_heartbeat, handler, beats=3, **_heartbeat_kwargs()
    )
    assert got >= 3


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

    got = run_heartbeat_until(
        registration.registration_heartbeat, handler, beats=4, **_heartbeat_kwargs()
    )
    assert got >= 4


def test_heartbeat_survives_client_construction_failure(monkeypatch):
    # httpx.Client(...) is constructed lazily INSIDE the guarded loop (first
    # beat) precisely so a construction failure (e.g. a broken SSL_CERT_FILE)
    # can't escape and cancel the lifespan task group. Simulate that: the first
    # construction attempt raises, the second succeeds — subsequent beats must
    # still fire. (No injected client here, so the loop builds its own.)
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
                await registration.registration_heartbeat(**_heartbeat_kwargs())

            tg.start_soon(_beat)
            with anyio.fail_after(5):
                while beat_count["n"] < 3:
                    await anyio.sleep(0.005)
            tg.cancel_scope.cancel()

    anyio.run(main)
    assert construct_calls["n"] == 2  # first raised, second succeeded
    assert beat_count["n"] >= 3


def test_heartbeat_confirms_first_beat_exactly_once(caplog):
    # One guaranteed INFO "registration confirmed" line on the first successful
    # beat (200 or 201), so a restart against an already-up platform (200, not
    # 201) is distinguishable from a heartbeat that never started. Steady-state
    # beats stay DEBUG — this must fire exactly once even over several beats.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "test"})

    with caplog.at_level(logging.INFO, logger="snowline_plugin_sdk.registration"):
        run_heartbeat_until(
            registration.registration_heartbeat,
            handler,
            beats=3,
            **_heartbeat_kwargs(),
        )

    confirmations = [
        r for r in caplog.records if "registration confirmed" in r.getMessage()
    ]
    assert len(confirmations) == 1


def test_heartbeat_interval_defaults_to_env_parse(monkeypatch):
    # `interval=None` (the production path) resolves through the shared lenient
    # env parse INSIDE the SDK — plugin wrappers pass interval straight through
    # instead of re-deriving the default. First beat fires immediately, so one
    # beat completes without waiting out the parsed interval.
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "30")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    got = run_heartbeat_until(
        registration.registration_heartbeat,
        handler,
        beats=1,
        **_heartbeat_kwargs(interval=None),
    )
    assert got >= 1


def test_heartbeat_rejects_nonfinite_direct_interval(monkeypatch, caplog):
    # A non-finite interval passed DIRECTLY (not via env) would make
    # anyio.sleep(inf) a silently dead heartbeat — the loop must warn and fall
    # back to the env parse instead of trusting it.
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "30")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with caplog.at_level(logging.WARNING, logger="snowline_plugin_sdk.registration"):
        got = run_heartbeat_until(
            registration.registration_heartbeat,
            handler,
            beats=1,
            **_heartbeat_kwargs(interval=float("inf")),
        )
    assert got >= 1
    assert any("invalid heartbeat interval" in r.getMessage() for r in caplog.records)


# --- heartbeat_seconds_from_env (the one shared deploy knob) -----------------


def test_heartbeat_interval_env_is_lenient(monkeypatch):
    # A malformed or hot-looping value in the SHARED env var must not kill the
    # heartbeat (a dead heartbeat = a hollow gateway after the next platform
    # restart) — warn and fall back instead.
    monkeypatch.delenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", raising=False)
    assert registration.heartbeat_seconds_from_env() == 15.0
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "15s")
    assert registration.heartbeat_seconds_from_env() == 15.0  # malformed → default
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "0")
    assert registration.heartbeat_seconds_from_env() == 1.0  # floored, no hot loop
    # "inf"/"nan"/"infinity" all parse as floats and slip past the bare `< 1.0`
    # floor, but anyio.sleep(inf/nan) never returns — a silently dead heartbeat,
    # the exact failure this lenient parse exists to prevent.
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "inf")
    assert registration.heartbeat_seconds_from_env() == 15.0  # non-finite → default
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "infinity")
    assert registration.heartbeat_seconds_from_env() == 15.0  # non-finite → default
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "nan")
    assert registration.heartbeat_seconds_from_env() == 15.0  # non-finite → default
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "30")
    assert registration.heartbeat_seconds_from_env() == 30.0


def test_heartbeat_interval_warnings_attribute_to_the_callers_logger(
    monkeypatch, caplog
):
    # The fallback warning must land on the PLUGIN's own logger when one is
    # passed (an operator watching `snowline_<plugin>.*` must see a
    # fat-fingered env var), defaulting to the SDK logger otherwise.
    monkeypatch.setenv("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS", "15s")
    plugin_log = logging.getLogger("snowline_testplugin.registration")
    with caplog.at_level(logging.WARNING):
        registration.heartbeat_seconds_from_env(log=plugin_log)
    assert any(
        r.name == "snowline_testplugin.registration"
        and "malformed" in r.getMessage()
        for r in caplog.records
    )


# --- HeartbeatHttpxLogFilter ------------------------------------------------


def test_httpx_filter_drops_only_heartbeat_post_plugins_lines():
    # The scoped httpx log filter (replacing the process-wide WARNING cap): it
    # must drop ONLY the heartbeat's `POST .../plugins` request lines, letting
    # every other httpx request trace through (scope reads, webhook delivery).
    log_filter = registration.HeartbeatHttpxLogFilter()

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
    scope_read_line = _record(
        'HTTP Request: GET http://platform.example/scopes/foo "HTTP/1.1 200 OK"'
    )
    webhook_post_line = _record(
        'HTTP Request: POST http://example.com/webhook "HTTP/1.1 200 OK"'
    )
    # A URL that merely CONTAINS "/plugins" mid-path (an outbound delivery to
    # some unrelated service) must still trace through — the filter anchors on
    # the path ENDING in /plugins.
    plugins_substring_line = _record(
        'HTTP Request: POST https://hub.example/api/plugins/events "HTTP/1.1 500 ..."'
    )

    assert log_filter.filter(heartbeat_line) is False
    assert log_filter.filter(scope_read_line) is True
    assert log_filter.filter(webhook_post_line) is True
    assert log_filter.filter(plugins_substring_line) is True


def test_install_heartbeat_httpx_filter_is_idempotent():
    # Repeated installs (per create_app, several plugins in one process) must
    # attach exactly ONE filter instance to the shared `httpx` logger.
    httpx_logger = logging.getLogger("httpx")
    before = list(httpx_logger.filters)
    try:
        registration.install_heartbeat_httpx_filter()
        registration.install_heartbeat_httpx_filter()
        added = [f for f in httpx_logger.filters if f not in before]
        assert len(added) <= 1  # <=: an app import may have installed it already
        assert sum(
            1
            for f in httpx_logger.filters
            if isinstance(f, registration.HeartbeatHttpxLogFilter)
        ) == 1
    finally:
        httpx_logger.filters = before
