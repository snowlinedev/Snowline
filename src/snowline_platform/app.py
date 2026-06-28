"""The Snowline platform app (skeleton).

For now it wires the trust layer onto a minimal surface: ``/health`` (exempt, for
liveness checks) and ``/whoami`` (echoes the resolved `Principal`, so the trust
gate is observable). The gateway + plugin registry that composes plugin MCP
surfaces and UIs builds on top of this.
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from snowline_platform import config
from snowline_platform.middleware import TrustMiddleware
from snowline_platform.trust import CidrTrustProvider, TrustResolver


def build_resolver() -> TrustResolver:
    # v1: one provider — the configurable trusted-CIDR network gate. An OAuth
    # provider would be PREPENDED here later (token-first, CIDR fallback).
    return TrustResolver([CidrTrustProvider(config.trusted_cidrs())])


def create_app() -> FastAPI:
    app = FastAPI(title="Snowline Platform")
    app.add_middleware(
        TrustMiddleware,
        resolver=build_resolver(),
        exempt_paths={"/health"},
    )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/whoami")
    async def whoami(request: Request) -> dict:
        principal = request.state.principal
        return {"id": principal.id, "source": principal.source}

    return app


app = create_app()
