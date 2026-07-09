"""Dynamic client registration (RFC 7591) — Claude.ai registers itself; we also
cover the manual-client fallback (a client registered once is retrievable by the
authorize step, which is what the "advanced settings" manual client-id path
relies on)."""

from __future__ import annotations

import anyio

from ._helpers import REDIRECT_URI, build_front, front_client, register_client


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
