"""The spoke's local failover front (issue #260, item 12065698).

A tiny always-on reverse proxy bound LOOPBACK-ONLY on the spoke (default
`127.0.0.1:8850` — the same port the hub's gateway binds, so a Claude Code
MCP entry of `http://127.0.0.1:8850/mcp` reads the same path as the hub's
`http://<hub>:8850/mcp`). Every request is forwarded VERBATIM to ONE upstream:

  * the HUB gateway (`SNOWLINE_FRONT_HUB_URL`) while the hub's `/health`
    answers 200 within the probe window, else
  * the LOCAL spoke platform (`SNOWLINE_FRONT_SPOKE_URL`, default
    `http://127.0.0.1:8848`).

The point (Sean, 2026-10-07): the MBP's Claude Code always talks to the hub
for the newest fixes, but when the mini loses power it must continue against
the MBP's local spoke WITHOUT anyone editing an MCP URL — the operator is
usually on a phone driving a remote-control session. Failover and failback
are automatic.

**Routing — a background prober with hysteresis, never per-request.** A
prober GETs `<hub>/health` every `probe_interval` seconds (default 5 s,
timeout 2 s). A probe SUCCEEDS iff it gets HTTP 200 — a `"status":
"degraded"` body is still a 200 and still routable (the platform's own
routability rule, health.py); a non-200, a connect failure or a timeout is a
FAILURE. The current upstream flips hub→spoke after `fail_threshold`
consecutive failures (default 2, ~10 s) and back spoke→hub only after
`recover_threshold` consecutive successes (default 6, ~30 s stable) — a hub
that is flapping keeps the front on the spoke rather than flapping with it.
Every flip is logged at WARNING with both counters. At boot the first probe
runs BEFORE the front serves anything and seeds the initial upstream directly
(hub when it answered, spoke when not) — a reboot with the hub down must not
spend the first `fail_threshold` probes sending writes into a dead hub.

**Stateless, per-request.** The upstream is read once at request START; a
request in flight when a flip happens finishes against its original upstream.
An upstream failure on a request is a straight 502 naming that upstream — the
front NEVER retries against the other one mid-request (no double writes; the
caller's own retry lands on whatever the router says by then). That is safe
because of deploy-continuity layer 1 (docs/specs/deploy-continuity.md §2):
every MCP surface is stateless, so there is no session to orphan when the
upstream changes under a client. Writes made against the spoke during an
outage converge via the existing bidirectional pairing
(replication-continuity §4, decision 0b8390f7) — nothing on the wire changes.

**Verbatim forwarding.** Method, raw path (percent-encoding and trailing
slash intact — `http_proxy._raw_route_path`), raw query, body bytes and
headers minus hop-by-hop. Unlike the gateway's plugin proxy this front is
TRANSPARENT: the caller's `authorization`/`cookie` go up and `set-cookie`
comes back, because the upstream IS the platform the caller meant to talk to.
`host` is the upstream's own (the caller's is carried in `X-Forwarded-Host`),
`content-length` is recomputed from the buffered body, any caller-supplied
`forwarded`/`x-forwarded-*` is dropped and NO `X-Forwarded-For` is added (the
spoke platform's uvicorn trusts proxy headers from loopback, so one would
replace its view of the peer), and `X-Snowline-Front: hub|spoke` is set on
both the forwarded request and the relayed response. Responses STREAM
(`aiter_raw`, one ASGI body message per upstream chunk) — an MCP
streamable-HTTP `text/event-stream` reply is relayed unbuffered. An absolute
`Location`/`Content-Location` at the chosen upstream's origin (Starlette's
`GET /mcp` → `307 …/mcp/` is one) is rewritten to the front's origin, so a
redirect-following client never leaves the front for the hub directly.

**Own routes.** Exactly one path is served by the front itself and never
proxied: `GET /front/health` (shape: `FrontState.health_body`). `/health`
itself is PROXIED like everything else, so "the hub's /health" read through
the front stays truthful about whichever upstream answered it (the
`X-Snowline-Front` response header says which).

**Debugging.** `SNOWLINE_FRONT_FORCE_UPSTREAM=hub|spoke` pins the upstream
(no flips; the prober still runs and still reports `hub_healthy`).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from snowline_platform.http_proxy import (
    HOP_BY_HOP_HEADERS,
    ProxyPathError,
    _raw_body,
    _raw_route_path,
    _rewrite_origin,
    upstream_url,
)
from snowline_platform.ui_api import read_capped_body

log = logging.getLogger("snowline_platform.front")

HUB = "hub"
SPOKE = "spoke"
UPSTREAMS = (HUB, SPOKE)

FRONT_HEALTH_PATH = "/front/health"
FRONT_HEADER = "x-snowline-front"

DEFAULT_BIND = "127.0.0.1:8850"
DEFAULT_SPOKE_URL = "http://127.0.0.1:8848"
DEFAULT_PROBE_INTERVAL = 5.0
DEFAULT_PROBE_TIMEOUT = 2.0
DEFAULT_FAIL_THRESHOLD = 2
DEFAULT_RECOVER_THRESHOLD = 6
DEFAULT_CONNECT_TIMEOUT = 5.0
# Per-READ timeout on a forwarded NON-streaming request (dashboard, plain
# HTTP contracts). `SNOWLINE_FRONT_READ_TIMEOUT=0` disables it. A request
# whose `Accept` names `text/event-stream` — every MCP request, GET and POST —
# gets NO read timeout: the platform's stateless MCP mount still serves the
# standalone `GET /mcp/` SSE stream, which Claude Code holds open, silent,
# for the whole session; a read timeout would cut it every N seconds. Keyed
# off the REQUEST because the response content-type is only known after the
# upstream answered. The connect timeout always applies.
DEFAULT_READ_TIMEOUT = 600.0
# The request body is buffered (so `content-length` is truthful and nothing
# is half-sent on a connect failure). MCP/dashboard traffic is small JSON;
# this is "no realistic request, every runaway one".
BODY_LIMIT = 32 * 1024 * 1024

STRIP_REQUEST_HEADERS: frozenset[str] = HOP_BY_HOP_HEADERS | {
    "host", "content-length",
    "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    FRONT_HEADER,
}
STRIP_RESPONSE_HEADERS: frozenset[str] = HOP_BY_HOP_HEADERS | {
    "server", "date", FRONT_HEADER,
}
REWRITE_ORIGIN_HEADERS: frozenset[str] = frozenset({"location", "content-location"})


class FrontConfigError(ValueError):
    """The front's environment is unusable — phrased for the operator."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FrontConfig:
    hub_url: str
    spoke_url: str = DEFAULT_SPOKE_URL
    probe_interval: float = DEFAULT_PROBE_INTERVAL
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT
    fail_threshold: int = DEFAULT_FAIL_THRESHOLD
    recover_threshold: int = DEFAULT_RECOVER_THRESHOLD
    force_upstream: str | None = None
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float | None = DEFAULT_READ_TIMEOUT

    def __post_init__(self) -> None:
        for name in ("hub_url", "spoke_url"):
            value = getattr(self, name)
            url = httpx.URL(value)
            if url.scheme not in ("http", "https") or not url.host:
                raise FrontConfigError(f"{name} {value!r} is not an absolute http(s) URL")
            if url.query or url.fragment:
                raise FrontConfigError(f"{name} {value!r} must not carry a query or fragment")
        if self.fail_threshold < 1 or self.recover_threshold < 1:
            raise FrontConfigError("fail/recover thresholds must be >= 1")
        if self.probe_interval <= 0 or self.probe_timeout <= 0:
            raise FrontConfigError("probe interval and timeout must be > 0")
        if self.force_upstream is not None and self.force_upstream not in UPSTREAMS:
            raise FrontConfigError(
                f"SNOWLINE_FRONT_FORCE_UPSTREAM={self.force_upstream!r} — expected "
                "'hub' or 'spoke' (or unset)"
            )

    def url_for(self, upstream: str) -> str:
        return self.hub_url if upstream == HUB else self.spoke_url

    @property
    def hub_health_url(self) -> str:
        return self.hub_url.rstrip("/") + "/health"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FrontConfig:
        env = os.environ if env is None else env
        hub = (env.get("SNOWLINE_FRONT_HUB_URL") or "").strip()
        if not hub:
            raise FrontConfigError(
                "SNOWLINE_FRONT_HUB_URL is not set — the hub gateway's URL "
                "(e.g. http://100.81.176.75:8850); `snowline stack sync` "
                "renders it into ~/.config/snowline/front.env from stack.json"
            )

        def _num(name: str, default, cast):
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                return cast(raw)
            except ValueError as exc:
                raise FrontConfigError(f"{name}={raw!r} is not a valid number") from exc

        read_timeout = _num("SNOWLINE_FRONT_READ_TIMEOUT", DEFAULT_READ_TIMEOUT, float)
        force = (env.get("SNOWLINE_FRONT_FORCE_UPSTREAM") or "").strip().lower() or None
        return cls(
            hub_url=hub,
            spoke_url=(env.get("SNOWLINE_FRONT_SPOKE_URL") or "").strip() or DEFAULT_SPOKE_URL,
            probe_interval=_num("SNOWLINE_FRONT_PROBE_INTERVAL", DEFAULT_PROBE_INTERVAL, float),
            probe_timeout=_num("SNOWLINE_FRONT_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT, float),
            fail_threshold=_num("SNOWLINE_FRONT_FAIL_THRESHOLD", DEFAULT_FAIL_THRESHOLD, int),
            recover_threshold=_num(
                "SNOWLINE_FRONT_RECOVER_THRESHOLD", DEFAULT_RECOVER_THRESHOLD, int
            ),
            force_upstream=force,
            read_timeout=read_timeout if read_timeout and read_timeout > 0 else None,
        )


def parse_bind(bind: str) -> tuple[str, int]:
    """`host:port` (or `[v6]:port`) -> (host, port), refusing anything that is
    not loopback: the front is a pre-auth listener that forwards the caller's
    credentials, so it must never be reachable off-box (roam runbook §2)."""
    raw = bind.strip()
    if raw.startswith("["):
        host, _, rest = raw[1:].partition("]")
        port_s = rest.lstrip(":")
    else:
        host, _, port_s = raw.rpartition(":")
    if not host or not port_s:
        raise FrontConfigError(f"SNOWLINE_FRONT_BIND={bind!r} is not host:port")
    try:
        port = int(port_s)
    except ValueError as exc:
        raise FrontConfigError(f"SNOWLINE_FRONT_BIND={bind!r} has a non-numeric port") from exc
    if host != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise FrontConfigError(
                f"SNOWLINE_FRONT_BIND={bind!r} is not a loopback address — the "
                "front binds loopback only (127.0.0.1 / ::1)"
            )
    return host, port


# --------------------------------------------------------------------------
# Routing state — a pure state machine; the prober feeds it.
# --------------------------------------------------------------------------


class FrontState:
    """Which upstream new requests go to, plus the counters behind it.

    `record(ok)` is the ONLY mutator and is clock-injected, so hysteresis is
    tested without a real prober or a real clock."""

    def __init__(self, config: FrontConfig, *, now: Callable[[], datetime] = _utcnow) -> None:
        self.config = config
        self._now = now
        self.upstream: str = config.force_upstream or HUB
        self.hub_healthy: bool = False
        self.last_probe_at: datetime | None = None
        self.last_flip_at: datetime | None = None
        self.flips = 0
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.seeded = False

    def current(self) -> str:
        return self.upstream

    def seed(self, ok: bool) -> None:
        """The boot probe: set the initial upstream straight from it (no
        hysteresis — there is no prior state to protect), and count it."""
        self._count(ok)
        if self.config.force_upstream is None:
            self.upstream = HUB if ok else SPOKE
        self.seeded = True
        log.warning(
            "front: boot probe of %s %s — starting on upstream=%s%s",
            self.config.hub_health_url,
            "ok" if ok else "FAILED",
            self.upstream,
            " (forced)" if self.config.force_upstream else "",
        )

    def record(self, ok: bool) -> str | None:
        """Account one probe result; returns the new upstream on a flip, else
        None."""
        self._count(ok)
        if self.config.force_upstream is not None:
            return None
        target: str | None = None
        if ok and self.upstream == SPOKE and self.consecutive_successes >= self.config.recover_threshold:
            target = HUB
        elif not ok and self.upstream == HUB and self.consecutive_failures >= self.config.fail_threshold:
            target = SPOKE
        if target is None:
            return None
        previous = self.upstream
        self.upstream = target
        self.flips += 1
        self.last_flip_at = self.last_probe_at
        log.warning(
            "front: FLIP %s -> %s (hub %s; consecutive_failures=%d "
            "consecutive_successes=%d; flips=%d)",
            previous,
            target,
            "healthy again" if ok else "unhealthy",
            self.consecutive_failures,
            self.consecutive_successes,
            self.flips,
        )
        return target

    def _count(self, ok: bool) -> None:
        self.last_probe_at = self._now()
        self.hub_healthy = ok
        if ok:
            self.consecutive_successes += 1
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            self.consecutive_successes = 0

    def health_body(self) -> dict:
        """The stable `/front/health` shape — read by the standby readiness
        surface (item 5bb2a79c) and the operator. Keys are additive-only."""
        return {
            "status": "ok",
            "upstream": self.upstream,
            "hub_url": self.config.hub_url,
            "spoke_url": self.config.spoke_url,
            "hub_healthy": self.hub_healthy,
            "last_probe_at": _iso(self.last_probe_at),
            "last_flip_at": _iso(self.last_flip_at),
            "flips": self.flips,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "forced": self.config.force_upstream,
            "fail_threshold": self.config.fail_threshold,
            "recover_threshold": self.config.recover_threshold,
        }


async def probe_hub(client: httpx.AsyncClient, config: FrontConfig) -> bool:
    """One `<hub>/health` probe: True iff HTTP 200 within the timeout. A
    degraded body is still a 200 and still routable."""
    try:
        resp = await client.get(config.hub_health_url, timeout=config.probe_timeout)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


async def probe_loop(
    state: FrontState,
    client: httpx.AsyncClient,
    *,
    sleep: Callable[[float], object] = asyncio.sleep,
) -> None:
    """Probe forever, feeding `state`. Never raises out (a bug in one
    iteration is logged and counted as a failed probe, not a dead prober)."""
    while True:
        await sleep(state.config.probe_interval)
        try:
            ok = await probe_hub(client, state.config)
        except Exception:  # the prober must outlive anything
            log.exception("front: probe raised; counting it as a failure")
            ok = False
        state.record(ok)


# --------------------------------------------------------------------------
# Forwarding
# --------------------------------------------------------------------------


def forward_request_headers(scope_headers: list[tuple[bytes, bytes]], upstream: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    caller_host: str | None = None
    has_accept_encoding = False
    for raw_name, raw_value in scope_headers:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        if name == "host":
            caller_host = value
        if name == "accept-encoding":
            has_accept_encoding = True
        if name in STRIP_REQUEST_HEADERS:
            continue
        out.append((name, value))
    # httpx would volunteer `accept-encoding: gzip, deflate`; the relay is raw,
    # so the upstream must see exactly the caller's preference (or none).
    if not has_accept_encoding:
        out.append(("accept-encoding", "identity"))
    if caller_host is not None:
        out.append(("x-forwarded-host", caller_host))
    out.append((FRONT_HEADER, upstream))
    return out


def relay_response_headers(
    upstream_resp: httpx.Response, upstream_base: str, front_origin: str, upstream: str
) -> list[tuple[bytes, bytes]]:
    """Every upstream header, repeated ones kept as separate lines (so
    multiple `set-cookie`s survive), minus hop-by-hop and the framing uvicorn
    regenerates; an absolute upstream-origin redirect is pointed back at the
    front."""
    out: list[tuple[bytes, bytes]] = []
    for name, value in upstream_resp.headers.multi_items():
        lname = name.lower()
        if lname in STRIP_RESPONSE_HEADERS:
            continue
        if lname in REWRITE_ORIGIN_HEADERS:
            value = _rewrite_origin(value, upstream_base, front_origin)
        out.append((lname.encode("latin-1"), value.encode("latin-1")))
    out.append((FRONT_HEADER.encode("latin-1"), upstream.encode("latin-1")))
    return out


def wants_event_stream(headers: Mapping[str, str]) -> bool:
    return "text/event-stream" in headers.get("accept", "").lower()


def read_timeout_for(config: FrontConfig, headers: Mapping[str, str]) -> float | None:
    """No read timeout for an event-stream request (see DEFAULT_READ_TIMEOUT);
    the configured one otherwise."""
    return None if wants_event_stream(headers) else config.read_timeout


async def relay_body(upstream_resp: httpx.Response, upstream: str):
    """`http_proxy._raw_body`, except an upstream failure AFTER the headers
    went out (a read timeout, a reset) just ends the stream with ONE warning
    — the status is already sent, so there is nothing better to tell the
    client, and an ASGI exception would dump a traceback per broken stream."""
    try:
        async for chunk in _raw_body(upstream_resp):
            yield chunk
    except httpx.HTTPError as exc:
        log.warning("front: upstream %s stream broke after headers: %r", upstream, exc)


def _front_origin(scope: Scope, headers: Mapping[str, str]) -> str:
    host = headers.get("host")
    if not host:
        server = scope.get("server")
        host = f"{server[0]}:{server[1]}" if server else "127.0.0.1"
    return f"{scope.get('scheme', 'http')}://{host}"


class FrontProxy:
    """The router fallback: everything that is not `/front/health`."""

    def __init__(self, state: FrontState, client_getter: Callable[[], httpx.AsyncClient]) -> None:
        self.state = state
        self._client = client_getter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Websockets are not part of any Snowline surface; refuse cleanly.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1003})
            return
        # The upstream is fixed HERE, at request start: a flip while this
        # request is in flight does not move it.
        upstream = self.state.current()
        response = await self._forward(Request(scope, receive), upstream)
        await response(scope, receive, send)

    async def _forward(self, request: Request, upstream: str):
        cfg = self.state.config
        base = cfg.url_for(upstream)
        try:
            target = upstream_url(
                base, _raw_route_path(request.scope), request.scope.get("query_string", b"")
            )
        except ProxyPathError as exc:
            return JSONResponse(
                {"detail": f"request path cannot be forwarded: {exc}", "upstream": upstream},
                status_code=400,
            )
        body = await read_capped_body(request, BODY_LIMIT)
        if body is None:
            return JSONResponse(
                {"detail": f"request body exceeds the {BODY_LIMIT}-byte front limit",
                 "upstream": upstream},
                status_code=413,
            )
        client = self._client()
        upstream_req = client.build_request(
            request.method,
            target,
            content=body,
            headers=forward_request_headers(request.scope["headers"], upstream),
            timeout=httpx.Timeout(
                cfg.connect_timeout, read=read_timeout_for(cfg, request.headers)
            ),
        )
        try:
            upstream_resp = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            # NO retry against the other upstream: stateless, never a double
            # write. The caller's retry lands wherever the router says then.
            log.warning("front: upstream %s (%s) failed for %s %s: %r",
                        upstream, base, request.method, target, exc)
            return JSONResponse(
                {
                    "detail": f"front upstream {upstream!r} unreachable: "
                    f"{type(exc).__name__}: {exc}",
                    "upstream": upstream,
                    "upstream_url": base,
                },
                status_code=502,
                headers={FRONT_HEADER: upstream},
            )
        response = StreamingResponse(
            relay_body(upstream_resp, upstream),
            status_code=upstream_resp.status_code,
            background=BackgroundTask(upstream_resp.aclose),
        )
        response.raw_headers = relay_response_headers(
            upstream_resp, base, _front_origin(request.scope, request.headers), upstream
        )
        return response


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------


def build_app(
    config: FrontConfig,
    *,
    client: httpx.AsyncClient | None = None,
    run_prober: bool = True,
    now: Callable[[], datetime] = _utcnow,
) -> Starlette:
    """The front ASGI app. `client` is injectable (tests hand in an
    `httpx.MockTransport`-backed one); when omitted one is created in the
    lifespan and closed with it. `run_prober=False` skips the boot probe and
    the background loop (tests drive `app.state.front.record()` directly)."""
    state = FrontState(config, now=now)
    holder: dict[str, httpx.AsyncClient] = {}
    if client is not None:
        holder["client"] = client

    def _client() -> httpx.AsyncClient:
        existing = holder.get("client")
        if existing is None:
            existing = holder["client"] = httpx.AsyncClient()
        return existing

    async def front_health(_request: Request) -> JSONResponse:
        return JSONResponse(state.health_body())

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        owned = client is None
        task: asyncio.Task | None = None
        if run_prober:
            state.seed(await probe_hub(_client(), config))
            task = asyncio.create_task(probe_loop(state, _client()), name="front-prober")
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if owned and "client" in holder:
                await holder["client"].aclose()

    app = Starlette(
        routes=[Route(FRONT_HEALTH_PATH, front_health, methods=["GET"])],
        lifespan=lifespan,
    )
    # Never let Starlette's redirect-slashes answer for the upstream: every
    # path but /front/health is the upstream's to route (incl. `/mcp` → 307).
    app.router.redirect_slashes = False
    app.router.default = FrontProxy(state, _client)
    app.state.front = state
    return app


def _configure_logging() -> None:
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


def create_app() -> Starlette:
    """uvicorn factory (`uvicorn --factory snowline_platform.front:create_app`)
    — the launchd plist's entry point; config from the environment."""
    _configure_logging()
    return build_app(FrontConfig.from_env())


def serve(bind: str | None = None) -> None:
    """`snowline front` / `python -m snowline_platform.front`: bind from
    `SNOWLINE_FRONT_BIND` (default 127.0.0.1:8850), loopback only."""
    import uvicorn

    host, port = parse_bind(bind or os.environ.get("SNOWLINE_FRONT_BIND") or DEFAULT_BIND)
    _configure_logging()
    app = build_app(FrontConfig.from_env())
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    try:
        serve()
    except FrontConfigError as exc:
        raise SystemExit(f"error: {exc}") from exc
