"""The /plugins HTTP surface, including that the trust gate protects it."""

from starlette.testclient import TestClient

from snowline_platform.app import create_app
from snowline_platform.trust import Principal, TrustResolver


class _AlwaysTrust:
    """A permissive trust provider so route tests get past the gate."""

    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _trusted_client() -> TestClient:
    return TestClient(create_app(resolver=TrustResolver([_AlwaysTrust()])))


_MANIFEST = {"name": "governance", "base_url": "http://127.0.0.1:8801"}


def test_register_list_delete_roundtrip():
    client = _trusted_client()

    r = client.post("/plugins", json=_MANIFEST)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "governance"
    assert body["status"] == "unknown"
    assert body["manifest"]["mcp_path"] == "/mcp"

    listed = client.get("/plugins").json()["plugins"]
    assert [p["name"] for p in listed] == ["governance"]

    assert client.delete("/plugins/governance").status_code == 204
    assert client.get("/plugins").json()["plugins"] == []


def test_reregister_is_idempotent_upsert():
    # POST is the registration heartbeat's verb (issue #39): re-POSTing the same
    # manifest is a 200 no-op, and a CHANGED manifest replaces the entry (a
    # redeploy that moved a plugin takes effect without an unregister).
    client = _trusted_client()
    assert client.post("/plugins", json=_MANIFEST).status_code == 201

    r = client.post("/plugins", json=_MANIFEST)
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "unchanged"

    moved = _MANIFEST | {"base_url": "http://127.0.0.1:9999"}
    r = client.post("/plugins", json=moved)
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "updated"
    listed = client.get("/plugins").json()["plugins"]
    assert [p["manifest"]["base_url"] for p in listed] == ["http://127.0.0.1:9999"]


def test_delete_missing_is_404():
    client = _trusted_client()
    assert client.delete("/plugins/ghost").status_code == 404


def test_invalid_manifest_is_422():
    client = _trusted_client()
    bad = {"name": "Bad Name", "base_url": "http://x"}
    assert client.post("/plugins", json=bad).status_code == 422


def test_trust_gate_blocks_registration():
    # Default app (real CIDR resolver); TestClient's peer is "testclient",
    # which is not in any trusted CIDR -> the gate rejects before the route.
    client = TestClient(create_app())
    assert client.post("/plugins", json=_MANIFEST).status_code == 403
    # /health stays reachable (exempt) for liveness checks.
    assert client.get("/health").status_code == 200
