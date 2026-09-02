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

    # The platform self-entry (decision 0503fff0) is always present in the
    # registry, so filter it out to assert on the PLUGIN that was registered.
    def _plugins(names):
        return [n for n in names if n != "platform"]

    listed = client.get("/plugins").json()["plugins"]
    assert _plugins(p["name"] for p in listed) == ["governance"]

    assert client.delete("/plugins/governance").status_code == 204
    assert _plugins(
        p["name"] for p in client.get("/plugins").json()["plugins"]
    ) == []


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
    # Filter the always-present platform self-entry (decision 0503fff0) to assert
    # on the governance plugin's replaced base_url.
    listed = [
        p for p in client.get("/plugins").json()["plugins"] if p["name"] != "platform"
    ]
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


# --- the manifest `ui` block (ui-shell.md §3) -------------------------------
#
# Model-level validation edge cases live in test_manifest.py; these prove the
# SAME rules apply at the HTTP boundary (POST /plugins -> 422), the surface a
# plugin's registration heartbeat actually hits.

_UI_MANIFEST = _MANIFEST | {
    "ui": {
        "contract_version": 1,
        "widgets": [
            {
                "id": "shadow-activity",
                "slot": "home",
                "kind": "stat",
                "title": "Open shadow branches",
                "data": "/ui-api/widgets/shadow-activity",
                "refresh_seconds": 30,
            }
        ],
        "pages": [
            {
                "id": "shadow-branches",
                "route": "/shadow",
                "title": "Shadow discussions",
                "nav": True,
                "kind": "table",
                "data": "/ui-api/pages/branches",
            },
            {
                "id": "shadow-branch",
                "route": "/shadow/{branch}",
                "nav": False,
                "kind": "thread",
                "data": "/ui-api/pages/branches/{branch}",
            },
        ],
    }
}


def test_valid_ui_block_registers_via_post():
    client = _trusted_client()
    r = client.post("/plugins", json=_UI_MANIFEST)
    assert r.status_code == 201, r.text
    ui = r.json()["manifest"]["ui"]
    assert ui["contract_version"] == 1
    assert [w["id"] for w in ui["widgets"]] == ["shadow-activity"]
    assert [p["id"] for p in ui["pages"]] == ["shadow-branches", "shadow-branch"]


def test_ui_block_duplicate_widget_id_is_422():
    client = _trusted_client()
    bad = _MANIFEST | {
        "ui": {
            "widgets": [
                {"id": "w1", "slot": "home", "kind": "stat", "data": "/ui-api/a"},
                {"id": "w1", "slot": "home", "kind": "stat", "data": "/ui-api/b"},
            ]
        }
    }
    assert client.post("/plugins", json=bad).status_code == 422


def test_ui_block_bad_data_prefix_is_422():
    client = _trusted_client()
    bad = _MANIFEST | {
        "ui": {
            "widgets": [
                {"id": "w1", "slot": "home", "kind": "stat", "data": "/mcp/a"},
            ]
        }
    }
    assert client.post("/plugins", json=bad).status_code == 422


def test_ui_block_bad_route_is_422():
    client = _trusted_client()
    bad = _MANIFEST | {
        "ui": {"pages": [{"id": "p1", "route": "no-leading-slash", "kind": "table", "data": "/ui-api/p1"}]}
    }
    assert client.post("/plugins", json=bad).status_code == 422


def test_ui_block_unknown_top_level_field_is_422():
    client = _trusted_client()
    bad = _MANIFEST | {"ui": {"widgets": [], "unexpected_field": True}}
    assert client.post("/plugins", json=bad).status_code == 422


def test_ui_block_unknown_kind_and_future_contract_version_register_ok():
    client = _trusted_client()
    ok = _MANIFEST | {
        "ui": {
            "contract_version": 999,
            "widgets": [
                {
                    "id": "w1",
                    "slot": "home",
                    "kind": "a-kind-the-shell-does-not-know-yet",
                    "data": "/ui-api/a",
                }
            ],
        }
    }
    r = client.post("/plugins", json=ok)
    assert r.status_code == 201, r.text
    assert r.json()["manifest"]["ui"]["contract_version"] == 999


# --- the manifest `replication` block (replication-continuity.md §4) --------
#
# Model-level validation edge cases live in test_replication_manifest.py;
# these prove the SAME rules apply at the HTTP boundary (POST /plugins ->
# 422), the surface a plugin's registration heartbeat actually hits.

_REPLICATION_MANIFEST = _MANIFEST | {
    "replication": {
        "contract_version": 2,
        "ingest_path": "/events/ingest",
        "events": ["decision.recorded", "decision.superseded"],
    }
}


def test_valid_replication_block_registers_via_post():
    client = _trusted_client()
    r = client.post("/plugins", json=_REPLICATION_MANIFEST)
    assert r.status_code == 201, r.text
    replication = r.json()["manifest"]["replication"]
    assert replication["contract_version"] == 2
    assert replication["ingest_path"] == "/events/ingest"
    assert replication["events"] == ["decision.recorded", "decision.superseded"]


def test_absent_replication_block_registers_via_post():
    client = _trusted_client()
    r = client.post("/plugins", json=_MANIFEST)
    assert r.status_code == 201, r.text
    assert r.json()["manifest"]["replication"] is None


def test_replication_block_missing_ingest_path_is_422():
    client = _trusted_client()
    bad = _MANIFEST | {"replication": {"contract_version": 2}}
    assert client.post("/plugins", json=bad).status_code == 422


def test_replication_block_unknown_top_level_field_is_422():
    client = _trusted_client()
    bad = _MANIFEST | {
        "replication": {
            "contract_version": 2,
            "ingest_path": "/events/ingest",
            "unexpected_field": True,
        }
    }
    assert client.post("/plugins", json=bad).status_code == 422


# --- the `http` block over the route (gateway.md §3a) ------------------------


_PM = {
    "name": "pm",
    "base_url": "http://127.0.0.1:8802",
    "http": [
        {
            "prefix": "/provider",
            "methods": ["GET", "POST"],
            "description": "musher work-item provider contract",
        }
    ],
}


def test_register_with_http_block_and_introspect_it():
    client = _trusted_client()
    r = client.post("/plugins", json=_PM)
    assert r.status_code == 201, r.text
    assert r.json()["manifest"]["http"] == [
        {
            "prefix": "/provider",
            "methods": ["GET", "POST"],
            "description": "musher work-item provider contract",
        }
    ]
    # The block rides GET /plugins too...
    listed = {p["name"]: p for p in client.get("/plugins").json()["plugins"]}
    assert listed["pm"]["manifest"]["http"][0]["prefix"] == "/provider"
    # ...and the flat operator view names the holder.
    assert client.get("/plugins/http-routes").json() == {
        "routes": [
            {
                "prefix": "/provider",
                "methods": ["GET", "POST"],
                "plugin": "pm",
                "description": "musher work-item provider contract",
            }
        ]
    }


def test_http_prefix_collision_is_409_naming_the_holder():
    client = _trusted_client()
    assert client.post("/plugins", json=_PM).status_code == 201
    impostor = {
        "name": "impostor",
        "base_url": "http://127.0.0.1:9999",
        # Nested under pm's prefix — collision, not a distinct surface.
        "http": [{"prefix": "/provider/work-items"}],
    }
    r = client.post("/plugins", json=impostor)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "/provider/work-items" in detail and "'pm'" in detail
    # The refusal is total: the impostor never joined the registry.
    assert "impostor" not in {
        p["name"] for p in client.get("/plugins").json()["plugins"]
    }


def test_holder_heartbeat_is_unaffected_by_a_refused_collision():
    client = _trusted_client()
    assert client.post("/plugins", json=_PM).status_code == 201
    other = {"name": "other", "base_url": "http://x:1", "http": [{"prefix": "/provider"}]}
    assert client.post("/plugins", json=other).status_code == 409
    # The holder keeps beating normally — a refused challenger must not
    # disturb the incumbent's registration.
    r = client.post("/plugins", json=_PM)
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "unchanged"


def test_reserved_http_prefix_is_422_at_the_route():
    client = _trusted_client()
    bad = _PM | {"name": "pm", "http": [{"prefix": "/plugins"}]}
    assert client.post("/plugins", json=bad).status_code == 422


def test_http_routes_is_empty_when_no_plugin_declares_one():
    client = _trusted_client()
    assert client.get("/plugins/http-routes").json() == {"routes": []}
