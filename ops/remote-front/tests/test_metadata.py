"""OAuth metadata endpoints — the discovery walk Claude.ai performs before it can
authenticate: RFC 9728 protected-resource metadata and RFC 8414 authorization-
server metadata (incl. the RFC 7591 registration endpoint)."""

from __future__ import annotations

import anyio

from ._helpers import ISSUER, RESOURCE, build_front, front_client


async def _get(app, path):
    async with front_client(app) as client:
        return await client.get(path)


def test_protected_resource_metadata_rfc9728():
    app = build_front()
    resp = anyio.run(_get, app, "/.well-known/oauth-protected-resource/mcp")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == RESOURCE
    # Points Claude.ai at THIS front as its own authorization server.
    assert body["authorization_servers"] == [ISSUER + "/"] or body[
        "authorization_servers"
    ] == [ISSUER]
    assert body["bearer_methods_supported"] == ["header"]


def test_authorization_server_metadata_rfc8414():
    app = build_front()
    resp = anyio.run(_get, app, "/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    body = resp.json()
    assert body["issuer"].rstrip("/") == ISSUER
    assert body["authorization_endpoint"] == ISSUER + "/authorize"
    assert body["token_endpoint"] == ISSUER + "/token"
    # DCR (RFC 7591) advertised, PKCE S256 required, code + refresh grants.
    assert body["registration_endpoint"] == ISSUER + "/register"
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in body["grant_types_supported"]
    assert "refresh_token" in body["grant_types_supported"]
    assert body["response_types_supported"] == ["code"]
