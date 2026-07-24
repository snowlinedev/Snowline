"""The /milestones HTTP surface (milestones.md §5) — behind the trust gate; the
read/resolve + create/lifecycle path out-of-process plugins (governance, PM)
use. Mirrors test_scopes_routes' conventions."""

from starlette.testclient import TestClient

from snowline_platform import milestones, scopes
from snowline_platform.app import create_app
from snowline_platform.db import session_scope
from snowline_platform.trust import Principal, TrustResolver


class _AlwaysTrust:
    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _trusted_client() -> TestClient:
    return TestClient(
        create_app(
            resolver=TrustResolver([_AlwaysTrust()]),
            migrate_on_startup=False,
        )
    )


def _seed():
    with session_scope() as s:
        scopes.create(s, slug="turtlesedge", name="TurtlesEdge", kind="org")
        scopes.create(
            s, slug="turtlesedge/turtletracks", name="TurtleTracks", kind="project"
        )
        milestones.create(
            s, anchor="turtlesedge/turtletracks", name="spanish-beta"
        )


def test_create_over_http(clean_db):
    with session_scope() as s:
        scopes.create(s, slug="turtlesedge", name="TE", kind="org")
        scopes.create(
            s, slug="turtlesedge/turtletracks", name="TT", kind="project"
        )
    client = _trusted_client()
    r = client.post(
        "/milestones",
        json={
            "anchor": "turtlesedge/turtletracks",
            "name": "spanish-beta",
            "outcome": "Spanish beta ships",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["address"] == "turtlesedge/turtletracks/spanish-beta"
    assert r.json()["status"] == "planned"
    # Duplicate-by-case → 409.
    dup = client.post(
        "/milestones",
        json={"anchor": "TurtlesEdge/TurtleTracks", "name": "Spanish-Beta"},
    )
    assert dup.status_code == 409, dup.text


def test_get_and_404(clean_db):
    _seed()
    client = _trusted_client()
    got = client.get("/milestones/turtlesedge/turtletracks/spanish-beta")
    assert got.status_code == 200, got.text
    assert got.json()["name"] == "spanish-beta"
    assert client.get("/milestones/turtlesedge/turtletracks/ghost").status_code == 404


def test_resolve_over_http_and_mixed_case(clean_db):
    _seed()
    client = _trusted_client()
    r = client.get(
        "/milestones/resolve",
        params={"ref": "TurtlesEdge/turtletracks/Spanish-Beta"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["address"] == "turtlesedge/turtletracks/spanish-beta"
    assert body["resolved_via_alias"] is False


def test_resolve_bare_with_context(clean_db):
    _seed()
    client = _trusted_client()
    r = client.get(
        "/milestones/resolve",
        params={"ref": "spanish-beta", "context": "turtlesedge/turtletracks"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["address"] == "turtlesedge/turtletracks/spanish-beta"


def test_resolve_unknown_404_with_suggestions(clean_db):
    _seed()
    client = _trusted_client()
    r = client.get(
        "/milestones/resolve",
        params={"ref": "turtlesedge/turtletracks/spanish-bta"},
    )
    assert r.status_code == 404, r.text
    detail = r.json()["detail"]
    assert any(
        s["address"] == "turtlesedge/turtletracks/spanish-beta"
        for s in detail["suggestions"]
    )


def test_resolve_batch(clean_db):
    _seed()
    client = _trusted_client()
    r = client.post(
        "/milestones/resolve-batch",
        json={
            "refs": [
                "turtlesedge/turtletracks/spanish-beta",
                "turtlesedge/turtletracks/nope",
            ]
        },
    )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    hit = results["turtlesedge/turtletracks/spanish-beta"]
    assert hit["address"] == "turtlesedge/turtletracks/spanish-beta"
    assert hit["status"] == "planned"
    assert hit["resolved_via_alias"] is False
    # A single miss does not fail the batch — it carries an error marker.
    assert "error" in results["turtlesedge/turtletracks/nope"]


def test_lifecycle_over_http(clean_db):
    _seed()
    client = _trusted_client()
    addr = "turtlesedge/turtletracks/spanish-beta"
    # achieve-on-planned → 409.
    assert client.post(f"/milestones/{addr}/achieve").status_code == 409
    assert client.post(f"/milestones/{addr}/activate").status_code == 200
    assert (
        client.post(
            f"/milestones/{addr}/achieve", json={"reason": "shipped"}
        ).status_code
        == 200
    )
    assert client.get(f"/milestones/{addr}").json()["status"] == "achieved"
    # Transition log surfaced over HTTP.
    log = client.get(f"/milestones/{addr}/transitions").json()["transitions"]
    assert [(t["from_status"], t["to_status"]) for t in log] == [
        ("planned", "active"),
        ("active", "achieved"),
    ]


def test_list_and_filters_over_http(clean_db):
    _seed()
    client = _trusted_client()
    rows = client.get(
        "/milestones", params={"anchor": "turtlesedge"}
    ).json()["milestones"]
    assert {r["address"] for r in rows} == {
        "turtlesedge/turtletracks/spanish-beta"
    }
    empty = client.get(
        "/milestones", params={"status": "achieved"}
    ).json()["milestones"]
    assert empty == []


def test_patch_is_partial_and_null_clears(clean_db):
    """PATCH only touches keys PRESENT in the body: updating the outcome must
    not clear an existing target_date; an explicit null clears; unknown keys
    are rejected."""
    _seed()
    client = _trusted_client()
    addr = "turtlesedge/turtletracks/spanish-beta"
    assert (
        client.patch(
            f"/milestones/{addr}", json={"target_date": "2026-09-01"}
        ).status_code
        == 200
    )
    r = client.patch(f"/milestones/{addr}", json={"outcome": "beta ships"})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "beta ships"
    assert r.json()["target_date"] == "2026-09-01"  # preserved, not cleared
    r = client.patch(f"/milestones/{addr}", json={"target_date": None})
    assert r.json()["target_date"] is None  # explicit null clears
    assert r.json()["outcome"] == "beta ships"
    assert (
        client.patch(f"/milestones/{addr}", json={"status": "achieved"}).status_code
        == 422
    )  # unknown key (and no lifecycle bypass through PATCH)
    assert (
        client.patch(
            f"/milestones/{addr}", json={"target_date": "not-a-date"}
        ).status_code
        == 422
    )


def test_malformed_address_is_404_not_500(clean_db):
    """A grammar-invalid address addresses nothing — every `{address}` route
    fails 404-clean, never a 500 out of the validators."""
    _seed()
    client = _trusted_client()
    bad = "turtlesedge/turtletracks/bad$name"
    assert client.get(f"/milestones/{bad}").status_code == 404
    assert client.post(f"/milestones/{bad}/activate").status_code == 404
    assert client.get(f"/milestones/{bad}/transitions").status_code == 404
    assert client.patch(f"/milestones/{bad}", json={"outcome": "x"}).status_code == 404


def test_milestones_behind_trust_gate(clean_db):
    client = TestClient(create_app(migrate_on_startup=False))
    assert client.get("/milestones").status_code == 403
