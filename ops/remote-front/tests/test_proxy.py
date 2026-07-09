"""The reverse-proxy half: the spec-correct 401s that trigger Claude.ai's OAuth
walk, the authenticated round-trip (session headers passed through both ways,
bearer stripped from the upstream hop), SSE streaming across the hop, and a clean
502 when the upstream is unreachable — never a hang."""

from __future__ import annotations

import anyio
import httpx

from ._helpers import (
    build_config,
    build_front,
    front_client,
    full_grant,
)


async def _unauth_post(app):
    async with front_client(app) as client:
        return await client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})


def test_unauthenticated_request_is_401_with_www_authenticate():
    app = build_front()
    resp = anyio.run(_unauth_post, app)
    assert resp.status_code == 401
    www = resp.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    # RFC 9728: the 401 carries the protected-resource metadata pointer that
    # kicks off Claude.ai's discovery.
    assert "resource_metadata=" in www
    assert "oauth-protected-resource/mcp" in www


async def _bad_token(app):
    async with front_client(app) as client:
        return await client.post(
            "/mcp",
            headers={"Authorization": "Bearer not-a-real-token"},
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        )


def test_garbage_bearer_token_is_401():
    app = build_front()
    resp = anyio.run(_bad_token, app)
    assert resp.status_code == 401
    assert "www-authenticate" in resp.headers


async def _expired_token(app):
    # Mint an already-expired access token with the app's own codec.
    token, _exp = app.state.provider._codec.mint(
        client_id="c", subject="owner", scopes=[], ttl=-10
    )
    async with front_client(app) as client:
        return await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        )


def test_expired_access_token_is_401():
    app = build_front()
    resp = anyio.run(_expired_token, app)
    assert resp.status_code == 401


async def _authed_post(app):
    async with front_client(app) as client:
        grant = await full_grant(client)
        access = grant["tokens"]["access_token"]
        return await client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {access}",
                "Mcp-Session-Id": "client-session-abc",
                "MCP-Protocol-Version": "2025-06-18",
            },
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        )


def test_authenticated_round_trip_passes_session_headers_and_strips_bearer():
    app = build_front()
    resp = anyio.run(_authed_post, app)
    assert resp.status_code == 200
    body = resp.json()
    received = body["received_headers"]
    # MCP session headers reached the upstream...
    assert received["mcp-session-id"] == "client-session-abc"
    assert received["mcp-protocol-version"] == "2025-06-18"
    # ...but our bearer did NOT (the upstream trusts the tailnet by position).
    assert "authorization" not in received
    # The request body was forwarded intact.
    assert '"tools/list"' in body["received_body"]
    # And the upstream's session header came back through the front.
    assert resp.headers["mcp-session-id"] == "upstream-session-xyz"


async def _authed_sse(app):
    async with front_client(app) as client:
        grant = await full_grant(client)
        access = grant["tokens"]["access_token"]
        return await client.get(
            "/mcp",
            headers={
                "Authorization": f"Bearer {access}",
                "Accept": "text/event-stream",
            },
        )


def test_sse_streaming_survives_the_hop():
    app = build_front()
    resp = anyio.run(_authed_sse, app)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["mcp-session-id"] == "upstream-session-sse"
    text = resp.text
    assert "data: chunk-0" in text
    assert "data: chunk-1" in text
    assert "data: chunk-2" in text


def _raising_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tailnet path down", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _upstream_down(app):
    async with front_client(app) as client:
        grant = await full_grant(client)
        access = grant["tokens"]["access_token"]
        return await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {access}"},
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        )


def test_upstream_unreachable_returns_clean_502_not_a_hang():
    app = build_front(upstream_client=_raising_client())
    resp = anyio.run(_upstream_down, app)
    assert resp.status_code == 502
    assert resp.json()["error"] == "upstream_unavailable"
