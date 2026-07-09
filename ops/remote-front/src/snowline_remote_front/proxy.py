"""The reverse-proxy half: forward Streamable-HTTP MCP to the upstream gateway.

This ASGI app is mounted BEHIND the bearer gate (see app.py), so it only ever
runs for a request that already carried a valid access token — nothing
unauthenticated reaches the tailnet. It is a transparent HTTP proxy to a single
fixed upstream URL (`REMOTE_FRONT_UPSTREAM`), preserving MCP's streaming
semantics:

  - POST (JSON-RPC request) → upstream POST; the upstream's response — a JSON
    body OR a ``text/event-stream`` SSE response — is streamed back verbatim.
  - GET (open the server→client SSE channel) → upstream GET, streamed back.
  - DELETE (session teardown) → forwarded.
  - The MCP session headers (`Mcp-Session-Id`, `MCP-Protocol-Version`) and
    `Last-Event-ID` are passed through in BOTH directions, so session affinity
    and SSE resumability survive the hop.

Upstream unreachable (primary down / tailnet severed) → a clean 502, never a
hang: the CONNECT is bounded (config) while the READ is unbounded (an SSE stream
is meant to stay open). This module imports neither fly nor tailscale — the
upstream is just a URL.
"""

from __future__ import annotations

import logging

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

log = logging.getLogger("snowline_remote_front.proxy")

# Hop-by-hop headers (RFC 7230 §6.1) plus ones the proxy must own itself. We do
# NOT forward the client's Authorization: the upstream gateway trusts the tailnet
# by network position (its CIDR gate), not our bearer — and our access token is
# meaningless to it. `host`/`content-length` are recomputed by httpx;
# `accept-encoding` is dropped so the upstream returns identity bytes we can
# stream through without re-encoding.
_STRIP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "authorization",
        "accept-encoding",
        "connection",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# On the way back, `content-type` is carried via StreamingResponse's media_type,
# and the framing headers are recomputed by the ASGI server — so we drop them to
# avoid duplicate/contradictory values. Everything else (notably `mcp-session-id`
# and `mcp-protocol-version`) passes through.
_STRIP_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "connection",
        "keep-alive",
        "transfer-encoding",
    }
)


def _forward_request_headers(headers) -> list[tuple[str, str]]:
    return [(k, v) for k, v in headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS]


def _forward_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _STRIP_RESPONSE_HEADERS}


class UpstreamProxy:
    """ASGI app that proxies one request to the fixed upstream URL.

    The httpx client is looked up from the ASGI app state at request time (set in
    the app lifespan), so a single pooled client — with the right connect/read
    timeouts — is shared across requests and torn down on shutdown."""

    def __init__(self, upstream_url: str) -> None:
        self._upstream_url = upstream_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        method = scope["method"]
        client: httpx.AsyncClient = scope["app"].state.upstream_client

        body = await request.body() if method in ("POST", "PUT", "PATCH") else None
        upstream_request = client.build_request(
            method,
            self._upstream_url,
            headers=_forward_request_headers(request.headers),
            content=body,
        )

        try:
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.RequestError as exc:
            # Primary down / tailnet path down: a clean upstream error, not a
            # hang (issue #120 acceptance).
            log.warning("remote-front: upstream %s unreachable: %s", self._upstream_url, exc)
            response = JSONResponse(
                {
                    "error": "upstream_unavailable",
                    "error_description": f"upstream gateway unreachable: {exc}",
                },
                status_code=502,
            )
            await response(scope, receive, send)
            return

        async def body_stream():
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            finally:
                await upstream_response.aclose()

        response = StreamingResponse(
            body_stream(),
            status_code=upstream_response.status_code,
            headers=_forward_response_headers(upstream_response.headers),
            media_type=upstream_response.headers.get("content-type"),
        )
        await response(scope, receive, send)


def build_upstream_client(*, connect_timeout: float) -> httpx.AsyncClient:
    """The shared upstream HTTP client. CONNECT is bounded so a dead upstream
    fails fast (clean 502); READ is unbounded so a long-lived SSE stream is never
    cut off mid-flight."""
    timeout = httpx.Timeout(connect=connect_timeout, read=None, write=30.0, pool=5.0)
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)
