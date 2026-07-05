"""The platform's replication SELF-MANIFEST endpoint (replication-continuity §8,
issue #95): the platform has no `/plugins` registry entry of its own, so it
self-describes its scope-stream contract next to its replication surfaces for
the pairing CLI to version-check like a plugin's manifest block.

This pins two things the pairing path depends on: the endpoint's SHAPE (the same
`replication` block a plugin declares) and its trusted-CIDR posture — it must be
tailnet-gated exactly like the sibling admin routes it sits beside, so a peer
outside SNOWLINE_TRUSTED_CIDRS is refused. The request pattern (peer IP via the
ASGI transport's `client=`) mirrors sdk/tests/test_replication_admin.py.
"""

from __future__ import annotations

import anyio
import httpx
from fastapi import FastAPI

from snowline_platform import replication
from snowline_plugin_sdk.contract import CONTRACT_VERSION

from ._replication_helpers import make_store

TAILNET_PEER = "100.64.0.7"
HOTEL_LAN_PEER = "203.0.113.9"


def _app() -> FastAPI:
    """A platform-shaped app over the REAL `replication.build_router` (manifest
    endpoint + ingest + admin surface), on a throwaway in-memory store."""
    _, _, scope = make_store()
    app = FastAPI()
    app.include_router(replication.build_router(scope))
    return app


def _request(app, method: str, path: str, *, peer=TAILNET_PEER):
    result = {}

    async def main():
        transport = httpx.ASGITransport(app=app, client=(peer, 4242))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://platform"
        ) as client:
            result["response"] = await client.request(method, path)

    anyio.run(main)
    return result["response"]


def test_self_manifest_shape_matches_the_replication_block():
    """The endpoint returns the same block shape a plugin declares (§4): a real
    contract_version + ingest_path + scope vocabulary, with advertised_base_url
    absent (a peer discovers the platform AT its base URL, §8)."""
    body = _request(_app(), "GET", replication.MANIFEST_PATH).json()
    assert body == {
        "contract_version": CONTRACT_VERSION,
        "ingest_path": replication.INGEST_PATH,
        "events": list(replication.SCOPE_EVENTS),
        "advertised_base_url": None,
    }
    # The scope stream's declared vocabulary is exactly what it applies/emits.
    assert body["events"] == ["scope.created", "scope.updated"]


def test_self_manifest_is_tailnet_gated_like_its_sibling_admin_routes():
    """Same trusted-CIDR posture as the sibling admin routes (§5.1): an
    untrusted hotel-LAN peer is refused; tailnet + loopback peers pass. Asserted
    side-by-side with a sibling admin route so the postures can't drift."""
    app = _app()
    for path in (replication.MANIFEST_PATH, f"{replication.ADMIN_PREFIX}/inbound"):
        assert _request(app, "GET", path, peer=HOTEL_LAN_PEER).status_code == 403, path
        for peer in (TAILNET_PEER, "127.0.0.1"):
            assert _request(app, "GET", path, peer=peer).status_code == 200, (path, peer)


def test_manifest_payload_helper_matches_the_endpoint():
    """`manifest_payload()` is the single source the endpoint serves — pin it so
    a drift between the helper (used by tests/tooling) and the route is caught."""
    assert replication.manifest_payload() == _request(
        _app(), "GET", replication.MANIFEST_PATH
    ).json()
