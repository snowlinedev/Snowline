"""The /scopes HTTP surface — behind the trust gate, fetching a scope's ancestor
chain and the tree over HTTP (the read path out-of-process plugins use)."""

from starlette.testclient import TestClient

from snowline_platform import scopes
from snowline_platform.app import create_app
from snowline_platform.db import session_scope
from snowline_platform.trust import Principal, TrustResolver


class _AlwaysTrust:
    """A permissive trust provider so route tests get past the gate."""

    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _trusted_client() -> TestClient:
    # migrate_on_startup=False: the conftest already provisioned the schema.
    return TestClient(
        create_app(
            resolver=TrustResolver([_AlwaysTrust()]),
            migrate_on_startup=False,
        )
    )


def _seed():
    with session_scope() as s:
        scopes.create(s, slug="org", name="Org", kind="org")
        scopes.create(s, slug="org/repo", name="Repo", kind="project", isolated=True)
        scopes.create(s, slug="org/repo/init", name="Init", kind="initiative")


def test_get_ancestors_over_http_halts_at_isolated(clean_db):
    _seed()
    client = _trusted_client()
    r = client.get("/scopes/org/repo/init/ancestors")
    assert r.status_code == 200, r.text
    chain = [a["slug"] for a in r.json()["ancestors"]]
    assert chain == ["org/repo/init", "org/repo"]  # stops at isolated org/repo


def test_get_tree_over_http(clean_db):
    _seed()
    client = _trusted_client()
    r = client.get("/scopes/tree")
    assert r.status_code == 200, r.text
    forest = r.json()["tree"]
    assert forest[0]["slug"] == "org"
    repo = forest[0]["children"][0]
    assert repo["slug"] == "org/repo"
    assert repo["isolated"] is True


def test_get_tree_with_root(clean_db):
    _seed()
    client = _trusted_client()
    r = client.get("/scopes/tree", params={"root": "org/repo"})
    assert r.status_code == 200, r.text
    assert r.json()["tree"][0]["slug"] == "org/repo"


def test_get_scope_and_404(clean_db):
    _seed()
    client = _trusted_client()
    assert client.get("/scopes/org/repo").json()["slug"] == "org/repo"
    assert client.get("/scopes/ghost").status_code == 404


def test_list_scopes_and_org_filter(clean_db):
    _seed()
    client = _trusted_client()
    slugs = [s["slug"] for s in client.get("/scopes").json()["scopes"]]
    assert slugs == ["org", "org/repo", "org/repo/init"]
    filtered = client.get("/scopes", params={"org": "org"}).json()["scopes"]
    assert len(filtered) == 3


def test_post_create_scope(clean_db):
    client = _trusted_client()
    r = client.post("/scopes", json={"slug": "acme", "name": "Acme", "kind": "org"})
    assert r.status_code == 201, r.text
    assert r.json()["slug"] == "acme"
    # bare-slug⇔org invariant violation -> 422
    bad = client.post(
        "/scopes", json={"slug": "bare", "name": "x", "kind": "project"}
    )
    assert bad.status_code == 422


def test_scopes_behind_trust_gate(clean_db):
    # Default app (real CIDR resolver); TestClient peer is untrusted -> 403.
    client = TestClient(create_app(migrate_on_startup=False))
    assert client.get("/scopes").status_code == 403
