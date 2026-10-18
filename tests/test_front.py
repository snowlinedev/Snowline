"""The spoke's loopback failover front (issue #260, `snowline_platform.front`).

Driven at the ASGI level with a hand-rolled `receive`/`send` pair (so the
tests can SEE each response-body message as it is sent — proving the relay is
unbuffered) and an `httpx.MockTransport` standing in for both upstreams. The
routing state machine is exercised directly with a fake clock; no real
prober, network, or sleep anywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from snowline_platform import front as f

HUB_URL = "http://100.81.176.75:8850"
SPOKE_URL = "http://127.0.0.1:8848"


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=5)
        return self.t


def _cfg(**kw) -> f.FrontConfig:
    return f.FrontConfig(hub_url=HUB_URL, spoke_url=SPOKE_URL, **kw)


# --------------------------------------------------------------------------
# Hysteresis (pure state machine)
# --------------------------------------------------------------------------


def test_flips_to_spoke_only_after_fail_threshold_consecutive_failures():
    st = f.FrontState(_cfg(), now=FakeClock())
    assert st.current() == f.HUB
    assert st.record(False) is None
    assert st.current() == f.HUB  # one blip never flips
    assert st.record(False) == f.SPOKE
    assert st.current() == f.SPOKE
    assert st.flips == 1
    assert st.last_flip_at == st.last_probe_at
    # further failures while on the spoke are not flips
    assert st.record(False) is None
    assert st.flips == 1


def test_a_success_between_failures_resets_the_failure_count():
    st = f.FrontState(_cfg(), now=FakeClock())
    st.record(False)
    st.record(True)
    assert st.record(False) is None
    assert st.current() == f.HUB


def test_flips_back_only_after_recover_threshold_stable_successes():
    st = f.FrontState(_cfg(), now=FakeClock())
    st.record(False)
    st.record(False)
    assert st.current() == f.SPOKE
    for _ in range(5):
        assert st.record(True) is None
    assert st.current() == f.SPOKE
    assert st.consecutive_successes == 5
    assert st.record(True) == f.HUB
    assert st.current() == f.HUB
    assert st.flips == 2


def test_a_flapping_hub_keeps_the_front_on_the_spoke():
    st = f.FrontState(_cfg(), now=FakeClock())
    st.record(False)
    st.record(False)
    for _ in range(20):  # up 5, down 1, forever: never 6 stable in a row
        for _ in range(5):
            st.record(True)
        st.record(False)
    assert st.current() == f.SPOKE
    assert st.flips == 1


def test_thresholds_are_configurable():
    st = f.FrontState(_cfg(fail_threshold=1, recover_threshold=2), now=FakeClock())
    assert st.record(False) == f.SPOKE
    assert st.record(True) is None
    assert st.record(True) == f.HUB


def test_boot_probe_seeds_the_initial_upstream_without_hysteresis():
    down = f.FrontState(_cfg(), now=FakeClock())
    down.seed(False)
    assert down.current() == f.SPOKE
    assert down.flips == 0 and down.seeded
    up = f.FrontState(_cfg(), now=FakeClock())
    up.seed(True)
    assert up.current() == f.HUB
    assert up.hub_healthy is True


def test_forced_upstream_never_flips_but_still_tracks_hub_health():
    st = f.FrontState(_cfg(force_upstream="spoke"), now=FakeClock())
    st.seed(True)
    assert st.current() == f.SPOKE
    for _ in range(10):
        assert st.record(True) is None
    assert st.current() == f.SPOKE
    assert st.hub_healthy is True
    assert st.health_body()["forced"] == "spoke"


def test_every_flip_is_logged_at_warning_with_both_counters(caplog):
    st = f.FrontState(_cfg(), now=FakeClock())
    with caplog.at_level(logging.WARNING, logger="snowline_platform.front"):
        st.record(False)
        st.record(False)
    flips = [r for r in caplog.records if "FLIP" in r.getMessage()]
    assert len(flips) == 1
    msg = flips[0].getMessage()
    assert flips[0].levelno == logging.WARNING
    assert "hub -> spoke" in msg
    assert "consecutive_failures=2" in msg and "consecutive_successes=0" in msg


def test_front_health_body_has_the_stable_shape():
    st = f.FrontState(_cfg(), now=FakeClock())
    body = st.health_body()
    assert body["last_probe_at"] is None and body["last_flip_at"] is None
    st.record(False)
    st.record(False)
    body = st.health_body()
    # the keys item 5bb2a79c reads (additive-only beyond these)
    for key, typ in {
        "status": str, "upstream": str, "hub_url": str, "spoke_url": str,
        "hub_healthy": bool, "last_probe_at": str, "last_flip_at": str,
        "flips": int, "consecutive_failures": int, "consecutive_successes": int,
    }.items():
        assert isinstance(body[key], typ), key
    assert body["status"] == "ok"
    assert body["upstream"] == "spoke"
    assert body["hub_url"] == HUB_URL and body["spoke_url"] == SPOKE_URL
    assert body["hub_healthy"] is False
    assert body["flips"] == 1
    assert body["consecutive_failures"] == 2 and body["consecutive_successes"] == 0
    datetime.fromisoformat(body["last_probe_at"])
    datetime.fromisoformat(body["last_flip_at"])


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------


def _probe(handler) -> bool:
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await f.probe_hub(client, _cfg())
    return asyncio.run(go())


def test_probe_is_healthy_on_200_including_a_degraded_body():
    assert _probe(lambda r: httpx.Response(200, json={"status": "ok"})) is True
    assert _probe(lambda r: httpx.Response(200, json={"status": "degraded"})) is True


def test_probe_hits_the_hubs_health_path():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200)
    _probe(handler)
    assert seen == [f"{HUB_URL}/health"]


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(503),
    lambda r: httpx.Response(500),
    lambda r: httpx.Response(404),
])
def test_probe_fails_on_non_200(handler):
    assert _probe(handler) is False


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_probe_fails_on_unreachable_or_timeout(exc):
    def handler(request):
        raise exc
    assert _probe(handler) is False


def test_lifespan_runs_the_boot_probe_and_the_background_prober():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)  # hub is down the whole time

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = f.build_app(_cfg(probe_interval=0.001), client=client)
        state = app.state.front
        async with app.router.lifespan_context(app):
            assert state.seeded and state.current() == f.SPOKE  # boot probe, no hysteresis
            for _ in range(200):
                if calls["n"] >= 4:
                    break
                await asyncio.sleep(0.005)
        await client.aclose()
        return state

    state = asyncio.run(go())
    assert calls["n"] >= 4
    assert state.consecutive_failures >= 4
    assert state.flips == 0  # seeded straight onto the spoke


# --------------------------------------------------------------------------
# ASGI driver
# --------------------------------------------------------------------------


class Exchange:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.first_body = asyncio.Event()

    async def send(self, message):
        self.messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            self.first_body.set()

    @property
    def status(self) -> int:
        return next(m for m in self.messages if m["type"] == "http.response.start")["status"]

    @property
    def headers(self) -> list[tuple[str, str]]:
        start = next(m for m in self.messages if m["type"] == "http.response.start")
        return [(k.decode(), v.decode()) for k, v in start["headers"]]

    def header(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name]

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")

    @property
    def body_chunks(self) -> list[bytes]:
        return [m["body"] for m in self.messages if m["type"] == "http.response.body" and m.get("body")]


async def call(app, method="GET", raw_path=b"/", query=b"", headers=None, body=b"", ex=None):
    """Drive one request; pass `ex` to watch an exchange still in flight."""
    ex = ex if ex is not None else Exchange()
    hdrs = [(b"host", b"127.0.0.1:8850")] + [
        (k.encode(), v.encode()) for k, v in (headers or [])
    ]
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": raw_path.decode(), "raw_path": raw_path, "query_string": query,
        "root_path": "", "headers": hdrs, "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8850),
    }
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    await app(scope, receive, ex.send)
    return ex


class Upstreams:
    """One MockTransport serving both upstreams, recording every request."""

    def __init__(self, hub=None, spoke=None) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self._hub = hub or (lambda r: httpx.Response(200, text="hub"))
        self._spoke = spoke or (lambda r: httpx.Response(200, text="spoke"))

    async def handler(self, request: httpx.Request):
        self.requests.append(request)
        self.bodies.append(await request.aread())
        target = self._hub if request.url.port == 8850 else self._spoke
        out = target(request)
        if asyncio.iscoroutine(out):
            out = await out
        return out

    def hits(self, port: int) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.port == port]

    def app(self, **cfg):
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return f.build_app(_cfg(**cfg), client=client, run_prober=False)


class GatedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], gate: asyncio.Event) -> None:
        self.chunks = chunks
        self.gate = gate

    async def __aiter__(self):
        yield self.chunks[0]
        await self.gate.wait()
        for chunk in self.chunks[1:]:
            yield chunk

    async def aclose(self) -> None:
        pass


# --------------------------------------------------------------------------
# Forwarding
# --------------------------------------------------------------------------


def test_forwards_method_raw_path_query_body_and_headers_verbatim():
    ups = Upstreams()

    async def go():
        app = ups.app()
        return await call(
            app, "POST", raw_path=b"/provider/a%2Fb/items/", query=b"x=1&x=2&y=%23",
            headers=[
                ("content-type", "application/json"),
                ("authorization", "Bearer tok"),
                ("cookie", "s=1"),
                ("mcp-session-id", "abc"),
                ("connection", "keep-alive, x-custom-hop"),
                ("keep-alive", "timeout=5"),
                ("te", "trailers"),
                ("x-forwarded-for", "6.6.6.6"),
                ("x-snowline-front", "spoke"),
                ("content-length", "999"),
            ],
            body=b'{"jsonrpc":"2.0"}',
        )

    ex = asyncio.run(go())
    assert ex.status == 200 and ex.body == b"hub"
    (req,) = ups.requests
    assert req.method == "POST"
    assert req.url.raw_path == b"/provider/a%2Fb/items/?x=1&x=2&y=%23"
    assert req.url.host == "100.81.176.75" and req.url.port == 8850
    assert ups.bodies == [b'{"jsonrpc":"2.0"}']
    h = req.headers
    assert h["host"] == "100.81.176.75:8850"  # the upstream's own
    assert h["x-forwarded-host"] == "127.0.0.1:8850"  # the caller's
    assert h["authorization"] == "Bearer tok"  # transparent front
    assert h["cookie"] == "s=1"
    assert h["mcp-session-id"] == "abc"
    assert h["content-type"] == "application/json"
    assert h["content-length"] == str(len(b'{"jsonrpc":"2.0"}'))  # recomputed
    assert h["x-snowline-front"] == "hub"  # ours, not the caller's
    assert h["accept-encoding"] == "identity"
    for gone in ("keep-alive", "te", "x-forwarded-for", "forwarded"):
        assert gone not in h
    assert h.get("connection", "keep-alive") == "keep-alive"  # httpx's own, not the caller's


def test_response_headers_relayed_with_hop_by_hop_stripped_and_cookies_kept():
    def hub(request):
        return httpx.Response(
            201,
            headers=[
                ("content-type", "application/json"),
                ("set-cookie", "a=1"),
                ("set-cookie", "b=2"),
                ("etag", '"v3"'),
                ("keep-alive", "timeout=5"),
                ("server", "uvicorn"),
                ("x-snowline-front", "forged"),
            ],
            content=b"{}",
        )
    ups = Upstreams(hub=hub)
    ex = asyncio.run(call(ups.app()))
    assert ex.status == 201
    assert ex.header("set-cookie") == ["a=1", "b=2"]
    assert ex.header("etag") == ['"v3"']
    assert ex.header("keep-alive") == []
    assert ex.header("server") == []
    assert ex.header("x-snowline-front") == ["hub"]


def test_absolute_upstream_redirect_is_rewritten_to_the_front_and_mcp_is_proxied_not_redirected():
    def hub(request):
        assert request.url.raw_path == b"/mcp"  # the front did NOT redirect it itself
        return httpx.Response(307, headers={"location": f"{HUB_URL}/mcp/"})
    ups = Upstreams(hub=hub)
    ex = asyncio.run(call(ups.app(), raw_path=b"/mcp"))
    assert ex.status == 307
    assert ex.header("location") == ["http://127.0.0.1:8850/mcp/"]


def test_spoke_redirects_are_rewritten_against_the_spoke_origin():
    ups = Upstreams(spoke=lambda r: httpx.Response(307, headers={"location": f"{SPOKE_URL}/ui/"}))
    app = ups.app(force_upstream="spoke")
    ex = asyncio.run(call(app, raw_path=b"/ui"))
    assert ex.header("location") == ["http://127.0.0.1:8850/ui/"]
    assert ex.header("x-snowline-front") == ["spoke"]


def test_health_is_proxied_but_front_health_is_served_by_the_front():
    ups = Upstreams(hub=lambda r: httpx.Response(200, json={"status": "degraded"}))

    async def go():
        app = ups.app()
        a = await call(app, raw_path=b"/health")
        b = await call(app, raw_path=b"/front/health")
        return a, b

    proxied, own = asyncio.run(go())
    assert json.loads(proxied.body) == {"status": "degraded"}
    assert proxied.header("x-snowline-front") == ["hub"]
    assert [r.url.path for r in ups.requests] == ["/health"]  # /front/health never went up
    body = json.loads(own.body)
    assert body["status"] == "ok" and body["upstream"] == "hub"
    assert set(body) >= {
        "status", "upstream", "hub_url", "spoke_url", "hub_healthy", "last_probe_at",
        "last_flip_at", "flips", "consecutive_failures", "consecutive_successes",
    }


def test_routes_to_the_spoke_after_a_flip():
    ups = Upstreams()

    async def go():
        app = ups.app()
        app.state.front.record(False)
        app.state.front.record(False)
        return await call(app, "POST", raw_path=b"/mcp/", body=b"x")

    ex = asyncio.run(go())
    assert ex.body == b"spoke"
    assert ex.header("x-snowline-front") == ["spoke"]
    assert ups.hits(8850) == [] and len(ups.hits(8848)) == 1


def test_upstream_failure_is_a_502_naming_the_upstream_with_no_cross_retry():
    def hub(request):
        raise httpx.ConnectError("connection refused")
    ups = Upstreams(hub=hub)
    ex = asyncio.run(call(ups.app(), "POST", raw_path=b"/mcp/", body=b"write"))
    assert ex.status == 502
    body = json.loads(ex.body)
    assert body["upstream"] == "hub"
    assert body["upstream_url"] == HUB_URL
    assert ex.header("x-snowline-front") == ["hub"]
    assert ups.hits(8848) == []  # never retried against the spoke
    assert len(ups.hits(8850)) == 1  # nor against the hub a second time


def test_sse_responses_stream_unbuffered():
    async def go():
        gate = asyncio.Event()

        def hub(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
                stream=GatedStream([b"event: message\ndata: 1\n\n", b"data: 2\n\n"], gate),
            )

        app = Upstreams(hub=hub).app()
        ex = Exchange()
        task = asyncio.create_task(call(app, "POST", raw_path=b"/mcp/", body=b"{}", ex=ex))
        # The first event reaches the client while the upstream is still
        # holding the second one back — no buffering to EOF.
        await asyncio.wait_for(ex.first_body.wait(), 1.0)
        assert not task.done()
        assert ex.body == b"event: message\ndata: 1\n\n"
        gate.set()
        await asyncio.wait_for(task, 1.0)
        return ex

    ex = asyncio.run(go())
    assert ex.header("content-type") == ["text/event-stream"]
    assert ex.header("cache-control") == ["no-cache"]
    assert ex.body_chunks == [b"event: message\ndata: 1\n\n", b"data: 2\n\n"]
    assert ex.messages[-1]["type"] == "http.response.body"
    assert ex.messages[-1].get("more_body", False) is False


def test_in_flight_request_keeps_its_upstream_across_a_flip():
    async def go():
        gate = asyncio.Event()

        def hub(request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=GatedStream([b"data: hub-1\n\n", b"data: hub-2\n\n"], gate),
            )

        ups = Upstreams(hub=hub)
        app = ups.app()
        first = Exchange()
        task = asyncio.create_task(call(app, "POST", raw_path=b"/mcp/", body=b"{}", ex=first))
        await asyncio.wait_for(first.first_body.wait(), 1.0)
        # the prober now sees the hub die -> flip, while `first` is mid-stream
        assert app.state.front.record(False) is None
        assert app.state.front.record(False) == f.SPOKE
        second = await call(app, "POST", raw_path=b"/mcp/", body=b"{}")
        gate.set()
        await asyncio.wait_for(task, 1.0)
        return first, second, ups

    first, second, ups = asyncio.run(go())
    assert first.body == b"data: hub-1\n\ndata: hub-2\n\n"
    assert first.header("x-snowline-front") == ["hub"]
    assert second.body == b"spoke"
    assert second.header("x-snowline-front") == ["spoke"]
    assert len(ups.hits(8850)) == 1 and len(ups.hits(8848)) == 1


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_config_from_env_defaults_and_overrides():
    cfg = f.FrontConfig.from_env({"SNOWLINE_FRONT_HUB_URL": HUB_URL})
    assert cfg.hub_url == HUB_URL
    assert cfg.spoke_url == "http://127.0.0.1:8848"
    assert (cfg.probe_interval, cfg.probe_timeout) == (5.0, 2.0)
    assert (cfg.fail_threshold, cfg.recover_threshold) == (2, 6)
    assert cfg.force_upstream is None
    assert cfg.read_timeout == 600.0

    cfg = f.FrontConfig.from_env({
        "SNOWLINE_FRONT_HUB_URL": HUB_URL,
        "SNOWLINE_FRONT_SPOKE_URL": "http://127.0.0.1:9999",
        "SNOWLINE_FRONT_PROBE_INTERVAL": "1",
        "SNOWLINE_FRONT_FAIL_THRESHOLD": "3",
        "SNOWLINE_FRONT_RECOVER_THRESHOLD": "10",
        "SNOWLINE_FRONT_FORCE_UPSTREAM": "Spoke",
        "SNOWLINE_FRONT_READ_TIMEOUT": "0",
    })
    assert cfg.spoke_url == "http://127.0.0.1:9999"
    assert cfg.probe_interval == 1.0
    assert (cfg.fail_threshold, cfg.recover_threshold) == (3, 10)
    assert cfg.force_upstream == "spoke"
    assert cfg.read_timeout is None


@pytest.mark.parametrize("env, match", [
    ({}, "SNOWLINE_FRONT_HUB_URL is not set"),
    ({"SNOWLINE_FRONT_HUB_URL": "mini:8850"}, "not an absolute"),
    ({"SNOWLINE_FRONT_HUB_URL": HUB_URL, "SNOWLINE_FRONT_FORCE_UPSTREAM": "both"}, "expected 'hub'"),
    ({"SNOWLINE_FRONT_HUB_URL": HUB_URL, "SNOWLINE_FRONT_FAIL_THRESHOLD": "two"}, "not a valid number"),
    ({"SNOWLINE_FRONT_HUB_URL": HUB_URL, "SNOWLINE_FRONT_RECOVER_THRESHOLD": "0"}, ">= 1"),
])
def test_config_from_env_refuses_bad_values(env, match):
    with pytest.raises(f.FrontConfigError, match=match):
        f.FrontConfig.from_env(env)


def test_bind_is_loopback_only():
    assert f.parse_bind("127.0.0.1:8850") == ("127.0.0.1", 8850)
    assert f.parse_bind("[::1]:8850") == ("::1", 8850)
    assert f.parse_bind("localhost:9000") == ("localhost", 9000)
    for bad in ("0.0.0.0:8850", "100.81.176.75:8850", "example.com:8850"):
        with pytest.raises(f.FrontConfigError, match="loopback"):
            f.parse_bind(bad)
    with pytest.raises(f.FrontConfigError):
        f.parse_bind("8850")


def test_cli_registers_the_front_verb(monkeypatch):
    from snowline_platform import cli

    seen = {}
    monkeypatch.setattr(f, "serve", lambda bind=None: seen.setdefault("bind", bind))
    assert cli.main(["front", "--bind", "127.0.0.1:9000"]) == 0
    assert seen["bind"] == "127.0.0.1:9000"
