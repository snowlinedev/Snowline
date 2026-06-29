"""Pure-ASGI middleware that gates every request through the `TrustResolver`.

It attaches the resolved `Principal` to ``request.state.principal`` (via the ASGI
``scope["state"]``) and rejects untrusted requests with 403 before they reach any
route. The peer IP is ``scope["client"][0]`` — the REAL source IP when the
platform binds the tailnet interface directly. (Behind ``tailscale serve`` the
trust signal would move to injected identity headers, which would be a second
`TrustProvider` — same seam.)

**Why pure-ASGI, not `BaseHTTPMiddleware` (issue #21):** the platform mounts the
gateway's MCP **streamable-HTTP** surfaces (`/mcp`, `/shadow/mcp`) behind this
gate. `BaseHTTPMiddleware` proxies the response through an internal memory stream
and is a poor fit for long-lived SSE / streaming / duplex transports — it can
buffer or stall them. A pure-ASGI middleware operates on ``scope``/``receive``/
``send`` and passes the downstream app's ``send`` through UNTOUCHED, so streaming
bodies flow byte-for-byte and there is no per-response buffering on the hot path.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from snowline_platform.trust import TrustResolver


class TrustMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        resolver: TrustResolver,
        exempt_paths: set[str] | None = None,
    ) -> None:
        self.app = app
        self._resolver = resolver
        self._exempt = exempt_paths or set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only gate connection scopes; lifespan (and anything else) passes through
        # untouched — the gate is about peers, not process events.
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        if scope.get("path") in self._exempt:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        peer_ip = client[0] if client else ""
        principal = self._resolver.resolve(peer_ip, Headers(scope=scope))
        if principal is None:
            await self._deny(scope, receive, send)
            return

        # Starlette's `request.state` reads from `scope["state"]`; set the
        # principal there so downstream routes see `request.state.principal`.
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)

    async def _deny(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject an untrusted peer: 403 for HTTP, a policy-violation close for a
        websocket (the platform has no websockets today, but gate both)."""
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse({"detail": "untrusted source"}, status_code=403)
        await response(scope, receive, send)
