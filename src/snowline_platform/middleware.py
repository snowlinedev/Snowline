"""ASGI middleware that gates every request through the `TrustResolver`.

It attaches the resolved `Principal` to ``request.state.principal`` and rejects
untrusted requests with 403 before they reach any route. The peer IP is
``request.client.host`` — the REAL source IP when the platform binds the tailnet
interface directly. (Behind ``tailscale serve`` the trust signal would move to
injected identity headers, which would be a second `TrustProvider` — same seam.)
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from snowline_platform.trust import TrustResolver


class TrustMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        resolver: TrustResolver,
        exempt_paths: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._resolver = resolver
        self._exempt = exempt_paths or set()

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._exempt:
            return await call_next(request)
        peer_ip = request.client.host if request.client else ""
        principal = self._resolver.resolve(peer_ip, request.headers)
        if principal is None:
            return JSONResponse({"detail": "untrusted source"}, status_code=403)
        request.state.principal = principal
        return await call_next(request)
