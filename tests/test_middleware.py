"""The pure-ASGI `TrustMiddleware` contract (issue #21).

Drives the middleware directly with fake ASGI scope/receive/send — no HTTP server
— so the gate logic is pinned independent of Starlette's request machinery: an
untrusted peer is 403'd WITHOUT the downstream app running; a trusted peer reaches
it with the principal on `scope["state"]`; exempt paths and non-connection scopes
pass straight through. A streaming pass-through test proves the middleware never
buffers the downstream response (the reason it's pure-ASGI, not BaseHTTPMiddleware).
"""

from __future__ import annotations

import anyio

from snowline_platform.middleware import TrustMiddleware
from snowline_platform.trust import Principal, TrustResolver


class _AllowFrom:
    """Trusts exactly one peer IP (the pluggable provider shape)."""

    def __init__(self, ok_ip: str) -> None:
        self._ok = ok_ip

    def resolve(self, peer_ip, headers):
        return Principal(id="owner", source="test") if peer_ip == self._ok else None


def _resolver(ok_ip: str) -> TrustResolver:
    return TrustResolver([_AllowFrom(ok_ip)])


async def _drain(app, scope) -> list[dict]:
    """Run an ASGI app with an empty request body, collecting sent messages."""
    sent: list[dict] = []
    received = iter([{"type": "http.request", "body": b"", "more_body": False}])

    async def receive():
        return next(received)

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _http_scope(path: str, ip: str | None) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "client": (ip, 12345) if ip else None,
        "state": {},
    }


def test_untrusted_peer_gets_403_and_downstream_never_runs():
    ran = {"downstream": False}

    async def downstream(scope, receive, send):
        ran["downstream"] = True

    mw = TrustMiddleware(downstream, resolver=_resolver("100.64.0.1"))
    sent = anyio.run(_drain, mw, _http_scope("/plugins", "8.8.8.8"))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 403
    assert ran["downstream"] is False  # rejected BEFORE the app


def test_trusted_peer_reaches_downstream_with_principal():
    seen = {}

    async def downstream(scope, receive, send):
        seen["principal"] = scope["state"]["principal"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = TrustMiddleware(downstream, resolver=_resolver("100.64.0.1"))
    sent = anyio.run(_drain, mw, _http_scope("/plugins", "100.64.0.1"))

    assert seen["principal"].id == "owner"
    assert any(m["type"] == "http.response.start" and m["status"] == 200 for m in sent)


def test_exempt_path_bypasses_the_gate():
    ran = {"downstream": False}

    async def downstream(scope, receive, send):
        ran["downstream"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = TrustMiddleware(
        downstream, resolver=_resolver("100.64.0.1"), exempt_paths={"/health"}
    )
    # Untrusted IP, but /health is exempt -> downstream runs anyway.
    anyio.run(_drain, mw, _http_scope("/health", "8.8.8.8"))
    assert ran["downstream"] is True


def test_lifespan_scope_passes_through():
    ran = {"downstream": False}

    async def downstream(scope, receive, send):
        ran["downstream"] = True

    mw = TrustMiddleware(downstream, resolver=_resolver("100.64.0.1"))

    async def go():
        async def receive():
            return {"type": "lifespan.startup"}

        async def send(m):
            pass

        await mw({"type": "lifespan"}, receive, send)

    anyio.run(go)
    assert ran["downstream"] is True  # lifespan is never gated


def test_streaming_response_is_not_buffered():
    """A trusted streaming downstream sends its chunks straight through the
    middleware — each body message arrives (no buffering/collapse). This is the
    property BaseHTTPMiddleware would jeopardize."""

    async def streamer(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for chunk in (b"a", b"b", b"c"):
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    mw = TrustMiddleware(streamer, resolver=_resolver("100.64.0.1"))
    sent = anyio.run(_drain, mw, _http_scope("/mcp", "100.64.0.1"))

    bodies = [m["body"] for m in sent if m["type"] == "http.response.body"]
    assert bodies == [b"a", b"b", b"c", b""]  # every chunk, in order, distinct
