"""Dynamic client registration (RFC 7591) — Claude.ai registers itself; we also
cover the manual-client fallback (a client registered once is retrievable by the
authorize step, which is what the "advanced settings" manual client-id path
relies on) and the registration cap (open DCR on a public endpoint is otherwise
a disk-fill vector — see REMOTE_FRONT_MAX_CLIENTS)."""

from __future__ import annotations

import anyio

from ._helpers import (
    REDIRECT_URI,
    build_config,
    build_front,
    front_client,
    full_grant,
    register_client,
)


async def _register(app):
    async with front_client(app) as client:
        return await register_client(client)


def test_dcr_issues_client_id_and_secret():
    app = build_front()
    reg = anyio.run(_register, app)
    assert reg["client_id"]
    assert reg["client_secret"]
    assert reg["redirect_uris"] == [REDIRECT_URI]
    # Registered client is persisted in the store and reloadable by client_id
    # (the manual client-id path reuses exactly this record).
    loaded = app.state.store.get_client(reg["client_id"])
    assert loaded is not None
    assert loaded.client_id == reg["client_id"]


async def _register_bad(app):
    async with front_client(app) as client:
        return await client.post("/register", json={"redirect_uris": []})


def test_dcr_rejects_missing_redirect_uris():
    app = build_front()
    resp = anyio.run(_register_bad, app)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


async def _register_three(app):
    async with front_client(app) as client:
        return [await register_client(client) for _ in range(3)]


def test_dcr_cap_evicts_oldest_tokenless_client():
    # At the cap, a NEW registration evicts the oldest client that holds no
    # live refresh token — a registration flood recycles bounded storage.
    app = build_front(config=build_config(max_clients=2))
    regs = anyio.run(_register_three, app)
    store = app.state.store
    assert store.get_client(regs[0]["client_id"]) is None  # oldest, evicted
    assert store.get_client(regs[1]["client_id"]) is not None
    assert store.get_client(regs[2]["client_id"]) is not None


async def _fill_with_active_then_register(app):
    async with front_client(app) as client:
        # Two clients, each holding a LIVE refresh token (full grants).
        await full_grant(client)
        await full_grant(client)
        # Cap reached and every stored client is active → refuse, never evict.
        return await client.post(
            "/register",
            json={
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )


def test_dcr_cap_refuses_when_all_clients_hold_live_tokens():
    app = build_front(config=build_config(max_clients=2))
    resp = anyio.run(_fill_with_active_then_register, app)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_client_metadata"
    assert "limit" in (body.get("error_description") or "")
