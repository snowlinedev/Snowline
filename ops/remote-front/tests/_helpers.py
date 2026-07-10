"""Shared test helpers for the remote front.

No Postgres, no fly, no tailnet: the whole suite runs the front as a plain ASGI
app in-process (httpx `ASGITransport`, no socket) against a stub upstream (also an
in-process ASGI app), mirroring the platform's own gateway tests. Async is driven
the repo way — sync test functions calling `anyio.run(...)`.

Fixtures return callables/values; the OAuth helpers below run the full discovery →
DCR → PKCE code → token dance so each flow test asserts one behavior on top of a
real end-to-end grant.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from snowline_remote_front.app import create_app
from snowline_remote_front.config import Config

ISSUER = "http://localhost:9000"
RESOURCE = "http://localhost:9000/mcp"
UPSTREAM = "http://upstream.test/remote/mcp"
UPSTREAM_PATH = "/remote/mcp"
OWNER_PASSWORD = "correct horse battery staple"
SIGNING_KEY = "test-signing-key-0123456789abcdef"
REDIRECT_URI = "http://localhost:1234/callback"


def build_config(**overrides) -> Config:
    kwargs = dict(
        issuer_url=ISSUER,
        resource_url=RESOURCE,
        upstream_url=UPSTREAM,
        owner_password=OWNER_PASSWORD,
        signing_key=SIGNING_KEY,
        access_ttl=900,
        refresh_ttl=3600,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def stub_upstream() -> Starlette:
    """A mock upstream gateway surface. POST echoes the received request headers
    (so a test can prove pass-through / stripping) and sets an `Mcp-Session-Id`
    response header; GET streams a short SSE body."""

    async def handle_post(request: Request) -> JSONResponse:
        body = await request.body()
        return JSONResponse(
            {
                "received_headers": {k.lower(): v for k, v in request.headers.items()},
                "received_body": body.decode() or None,
            },
            headers={
                "Mcp-Session-Id": "upstream-session-xyz",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )

    async def handle_get(request: Request) -> StreamingResponse:
        async def events():
            for i in range(3):
                yield f"event: message\ndata: chunk-{i}\n\n".encode()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Mcp-Session-Id": "upstream-session-sse"},
        )

    return Starlette(
        routes=[
            Route(UPSTREAM_PATH, handle_post, methods=["POST"]),
            Route(UPSTREAM_PATH, handle_get, methods=["GET"]),
        ]
    )


def upstream_client_for(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://upstream.test"
    )


def build_front(*, upstream_app: Starlette | None = None, config: Config | None = None,
                store=None, upstream_client: httpx.AsyncClient | None = None) -> Starlette:
    """A front app wired to an in-process upstream (or an injected client, e.g. a
    MockTransport that raises, for the upstream-down test)."""
    config = config or build_config()
    if upstream_client is None:
        upstream_client = upstream_client_for(upstream_app or stub_upstream())
    return create_app(config, store=store, upstream_client=upstream_client)


def front_client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=ISSUER,
        follow_redirects=False,
    )


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


async def register_client(
    client: httpx.AsyncClient, *, client_name: str | None = None
) -> dict:
    metadata = {
        "redirect_uris": [REDIRECT_URI],
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if client_name is not None:
        metadata["client_name"] = client_name
    resp = await client.post("/register", json=metadata)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def authorize_to_txn(
    client: httpx.AsyncClient, *, client_id: str, challenge: str
) -> str:
    """Run just the /authorize half and return the parked login txn."""
    authz = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "state-123",
            "resource": RESOURCE,
        },
    )
    assert authz.status_code == 302, authz.text
    return parse_qs(urlparse(authz.headers["location"]).query)["txn"][0]


async def authorize_to_code(
    client: httpx.AsyncClient, *, client_id: str, challenge: str, password: str = OWNER_PASSWORD
) -> httpx.Response:
    """Run /authorize → /login(POST). Returns the final /login POST response
    (a 302 to the redirect_uri on success, or the error page)."""
    authz = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "state-123",
            "resource": RESOURCE,
        },
    )
    assert authz.status_code == 302, authz.text
    txn = parse_qs(urlparse(authz.headers["location"]).query)["txn"][0]
    return await client.post("/login", data={"txn": txn, "password": password})


async def full_grant(client: httpx.AsyncClient) -> dict:
    """Complete discovery→DCR→PKCE→token and return the token JSON plus the
    client credentials + verifier used (so refresh tests can reuse them)."""
    registration = await register_client(client)
    verifier, challenge = pkce_pair()
    login = await authorize_to_code(
        client, client_id=registration["client_id"], challenge=challenge
    )
    assert login.status_code == 302, login.text
    code = parse_qs(urlparse(login.headers["location"]).query)["code"][0]

    token_resp = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
    )
    assert token_resp.status_code == 200, token_resp.text
    return {"tokens": token_resp.json(), "registration": registration}
