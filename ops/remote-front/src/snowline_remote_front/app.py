"""Assemble the remote-front ASGI app.

Wiring, top to bottom:

  - `create_protected_resource_routes` (RFC 9728) — /.well-known/oauth-protected-resource/<path>
  - `create_auth_routes` (RFC 8414 + 7591) — AS metadata, /authorize, /token,
    /register (DCR), /revoke
  - the single-user /login page
  - the protected MCP proxy, mounted at the resource path behind
    `RequireAuthMiddleware` (the spec-correct 401 + WWW-Authenticate that triggers
    Claude.ai's discovery walk)
  - app-wide `AuthenticationMiddleware(BearerAuthBackend(...))` that populates the
    request principal from the bearer token; only the proxy mount ENFORCES it.

This is a plain ASGI app: no fly, no tailscale, no Snowline platform imports. It
runs and is fully testable against a mock upstream.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route

from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions

from snowline_remote_front.config import Config
from snowline_remote_front.login import login_routes
from snowline_remote_front.provider import RemoteFrontProvider
from snowline_remote_front.proxy import UpstreamProxy, build_upstream_client
from snowline_remote_front.store import InMemoryStore, SqliteStore, Store
from snowline_remote_front.tokens import AccessTokenCodec

log = logging.getLogger("snowline_remote_front.app")


def create_app(
    config: Config,
    *,
    store: Store | None = None,
    upstream_client=None,
) -> Starlette:
    """Build the ASGI app for `config`.

    `store` / `upstream_client` are injection seams for tests (an in-memory store
    and an httpx client bound to a stub upstream); in deploy both default from
    config — a SqliteStore when `store_path` is set, else in-memory, and a pooled
    upstream client with the configured connect timeout."""
    if store is None:
        store = SqliteStore(config.store_path) if config.store_path else InMemoryStore()

    codec = AccessTokenCodec(config.signing_key)
    provider = RemoteFrontProvider(
        store=store,
        codec=codec,
        issuer_url=config.issuer_url,
        subject=config.subject,
        access_ttl=config.access_ttl,
        refresh_ttl=config.refresh_ttl,
    )
    verifier = ProviderTokenVerifier(provider)

    issuer_url = AnyHttpUrl(config.issuer_url)
    resource_url = AnyHttpUrl(config.resource_url)

    # RFC 9728 protected-resource metadata: points Claude.ai at the AS (issuer).
    prm_routes = create_protected_resource_routes(
        resource_url=resource_url,
        authorization_servers=[issuer_url],
        resource_name="Snowline remote MCP",
    )
    # RFC 8414 AS metadata + /authorize + /token + /register (DCR) + /revoke.
    auth_routes = create_auth_routes(
        provider=provider,
        issuer_url=issuer_url,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )

    # The resource-metadata URL advertised in the 401 WWW-Authenticate, so a
    # bare/expired/bad token bounces Claude.ai into the discovery walk.
    resource_metadata_url = build_resource_metadata_url(resource_url)

    # The protected proxy, gated by RequireAuthMiddleware. required_scopes=[] —
    # single resource owner, any valid token passes (scope isn't an authorization
    # axis here); the gate is simply "a valid access token, or nothing reaches
    # the tailnet". It is served as a Route at the EXACT resource path (not a
    # Mount, which would force a /mcp/ sub-path + trailing-slash redirect):
    # Starlette serves a non-function endpoint as a raw ASGI app, so the
    # auth+proxy chain runs directly on GET/POST/DELETE to /mcp.
    proxy = RequireAuthMiddleware(
        UpstreamProxy(config.upstream_url),
        required_scopes=[],
        resource_metadata_url=resource_metadata_url,
    )

    routes = [
        *prm_routes,
        *auth_routes,
        *login_routes(provider, config.owner_password),
        Route(
            config.resource_path,
            endpoint=proxy,
            methods=["GET", "POST", "DELETE"],
        ),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.upstream_client = upstream_client or build_upstream_client(
            connect_timeout=config.upstream_connect_timeout
        )
        try:
            yield
        finally:
            # Only close a client we created; an injected one is the caller's.
            if upstream_client is None:
                await app.state.upstream_client.aclose()

    app = Starlette(
        routes=routes,
        middleware=[Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(verifier))],
        lifespan=lifespan,
    )
    # Expose for tests + set the injected upstream client eagerly: httpx's
    # ASGITransport does NOT run lifespan events, so an injected client (tests)
    # must be reachable without entering the lifespan. In deploy `upstream_client`
    # is None here and the lifespan builds + owns the pooled client.
    app.state.provider = provider
    app.state.store = store
    if upstream_client is not None:
        app.state.upstream_client = upstream_client
    return app
