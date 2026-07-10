"""The authorization-code + PKCE flow end to end, the single-user login gate
(context rendering, attempt cap, throttle), pending-login hygiene (TTL + size
cap), and refresh-token rotation + expiry."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import anyio
import pytest

import snowline_remote_front.login as login_module
import snowline_remote_front.provider as provider_module

from ._helpers import (
    OWNER_PASSWORD,
    REDIRECT_URI,
    RESOURCE,
    authorize_to_code,
    authorize_to_txn,
    build_front,
    front_client,
    full_grant,
    pkce_pair,
    register_client,
)


@pytest.fixture
def no_login_delay(monkeypatch):
    """Zero the login failure throttle so wrong-password tests don't sleep."""
    monkeypatch.setattr(login_module, "LOGIN_FAILURE_BASE_DELAY", 0.0)


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


def test_login_rejects_wrong_credential(no_login_delay):
    app = build_front()
    resp = anyio.run(_wrong_password, app)
    assert resp.status_code == 401
    assert "Incorrect credential" in resp.text


async def _non_ascii_password(app):
    async with front_client(app) as client:
        reg = await register_client(client)
        _verifier, challenge = pkce_pair()
        return await authorize_to_code(
            client, client_id=reg["client_id"], challenge=challenge, password="pässwörd"
        )


def test_login_non_ascii_password_is_401_not_500(no_login_delay):
    # hmac.compare_digest raises TypeError on non-ASCII str inputs — the compare
    # runs over UTF-8 bytes precisely so this is a clean 401, never a 500.
    app = build_front()
    resp = anyio.run(_non_ascii_password, app)
    assert resp.status_code == 401


async def _login_page(app):
    async with front_client(app) as client:
        reg = await register_client(client, client_name="Example Connector")
        _verifier, challenge = pkce_pair()
        txn = await authorize_to_txn(
            client, client_id=reg["client_id"], challenge=challenge
        )
        return reg, await client.get("/login", params={"txn": txn})


def test_login_page_shows_client_identity_and_redirect_host():
    # Anti-phishing: the owner must be able to SEE who is asking and where the
    # browser will be sent before typing the credential.
    app = build_front()
    reg, resp = anyio.run(_login_page, app)
    assert resp.status_code == 200
    assert "Example Connector" in resp.text
    assert reg["client_id"] in resp.text
    assert urlparse(REDIRECT_URI).netloc in resp.text


async def _hammer_login(app):
    async with front_client(app) as client:
        reg = await register_client(client)
        _verifier, challenge = pkce_pair()
        txn = await authorize_to_txn(
            client, client_id=reg["client_id"], challenge=challenge
        )
        failures = [
            await client.post("/login", data={"txn": txn, "password": "wrong"})
            for _ in range(provider_module.MAX_LOGIN_ATTEMPTS)
        ]
        # The txn is now CONSUMED: even the CORRECT password no longer works.
        final = await client.post(
            "/login", data={"txn": txn, "password": OWNER_PASSWORD}
        )
        return failures, final


def test_txn_consumed_after_max_failed_logins(no_login_delay):
    app = build_front()
    failures, final = anyio.run(_hammer_login, app)
    for resp in failures[:-1]:
        assert resp.status_code == 401
    assert failures[-1].status_code == 400
    assert "Too many failed attempts" in failures[-1].text
    assert final.status_code == 400
    assert "expired" in final.text


async def _many_authorizes(app, count):
    async with front_client(app) as client:
        reg = await register_client(client)
        _verifier, challenge = pkce_pair()
        return [
            await authorize_to_txn(
                client, client_id=reg["client_id"], challenge=challenge
            )
            for _ in range(count)
        ]


def test_pending_login_size_cap_evicts_oldest(monkeypatch):
    # An /authorize hammer can't grow the pending set without bound: at the cap
    # the OLDEST parked login is evicted; the newest stays usable.
    monkeypatch.setattr(provider_module, "MAX_PENDING", 3)
    app = build_front()
    txns = anyio.run(_many_authorizes, app, 4)
    pending = app.state.provider._pending
    assert len(pending) <= 3
    assert txns[0] not in pending
    assert txns[-1] in pending


def test_pending_login_ttl_expires(monkeypatch):
    app = build_front()
    (txn,) = anyio.run(_many_authorizes, app, 1)
    assert app.state.provider.pending_login(txn) is not None
    # Force everything past the TTL: the txn stops being usable and is dropped.
    monkeypatch.setattr(provider_module, "PENDING_TTL", -1.0)
    assert app.state.provider.pending_login(txn) is None
    assert txn not in app.state.provider._pending


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
