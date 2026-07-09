"""The authorization-code + PKCE flow end to end, the single-user login gate, and
refresh-token rotation + expiry."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import anyio

from ._helpers import (
    REDIRECT_URI,
    RESOURCE,
    authorize_to_code,
    build_front,
    front_client,
    full_grant,
    pkce_pair,
    register_client,
)


async def _full(app):
    async with front_client(app) as client:
        return await full_grant(client)


def test_pkce_code_flow_yields_tokens():
    app = build_front()
    result = anyio.run(_full, app)
    tokens = result["tokens"]
    assert tokens["token_type"] == "Bearer"
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["expires_in"] == 900
    # The issued access token verifies locally as the single resource owner.
    verified = app.state.provider._codec.verify(tokens["access_token"])
    assert verified is not None
    assert verified.subject == "owner"


async def _wrong_password(app):
    async with front_client(app) as client:
        reg = await register_client(client)
        _verifier, challenge = pkce_pair()
        return await authorize_to_code(
            client, client_id=reg["client_id"], challenge=challenge, password="nope"
        )


def test_login_rejects_wrong_credential():
    app = build_front()
    resp = anyio.run(_wrong_password, app)
    assert resp.status_code == 401
    assert "Incorrect credential" in resp.text


async def _bad_pkce(app):
    async with front_client(app) as client:
        reg = await register_client(client)
        _verifier, challenge = pkce_pair()
        login = await authorize_to_code(
            client, client_id=reg["client_id"], challenge=challenge
        )
        code = parse_qs(urlparse(login.headers["location"]).query)["code"][0]
        # Exchange with the WRONG verifier — PKCE must fail (SDK-side check).
        return await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": reg["client_id"],
                "client_secret": reg["client_secret"],
                "code_verifier": "the-wrong-verifier-entirely",
                "resource": RESOURCE,
            },
        )


def test_token_rejects_bad_pkce_verifier():
    app = build_front()
    resp = anyio.run(_bad_pkce, app)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def _refresh(app):
    async with front_client(app) as client:
        grant = await full_grant(client)
        reg = grant["registration"]
        old_refresh = grant["tokens"]["refresh_token"]
        first = await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
                "client_id": reg["client_id"],
                "client_secret": reg["client_secret"],
            },
        )
        # Reuse of the ROTATED (old) refresh token must now fail.
        replay = await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
                "client_id": reg["client_id"],
                "client_secret": reg["client_secret"],
            },
        )
        return first, replay


def test_refresh_rotates_and_old_token_is_invalidated():
    app = build_front()
    first, replay = anyio.run(_refresh, app)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["access_token"]
    assert body["refresh_token"]
    # Rotation: the new refresh token differs from the old one.
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


async def _expired_refresh(app):
    async with front_client(app) as client:
        grant = await full_grant(client)
        reg = grant["registration"]
        refresh = grant["tokens"]["refresh_token"]
        # Force the stored refresh token to be already expired.
        stored = app.state.store.get_refresh_token(refresh)
        stored.expires_at = 1  # 1970
        app.state.store.put_refresh_token(stored)
        return await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": reg["client_id"],
                "client_secret": reg["client_secret"],
            },
        )


def test_expired_refresh_token_is_rejected():
    app = build_front()
    resp = anyio.run(_expired_refresh, app)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
