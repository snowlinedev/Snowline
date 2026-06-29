"""Artifact-graph behavior — register(inline)/get, revise/leaves, resolve
competing leaves, set_governs/set_maturity, the git-backend rejection, and the
ancestor-inherited `applicable_artifacts` against a STUBBED scope client.

DB-backed (skips cleanly when Postgres is unavailable). The scope dependency is
always a stub — these unit tests never require a running platform. Governs edges
key on the STABLE `scope_id` (#11), so the resolution maps the tests build use
the SAME per-slug id `StubScopeClient` serves.
"""

from __future__ import annotations

import uuid

import pytest

from snowline_governance import artifacts


def _sid(slug: str) -> uuid.UUID:
    """The stable per-slug scope id — IDENTICAL to `StubScopeClient`'s, so a
    governs edge keyed on this id matches the chain rows the stub's `ancestors`
    serves (governs-matching keys on `scope_id`, not the mutable slug)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


def _scope_row(slug: str) -> dict:
    """The platform `to_row`-shaped resolution one slug resolves to — what the MCP
    surface passes into `resolved_scopes`."""
    return {"id": str(_sid(slug)), "slug": slug}


def _resolved(*slugs: str) -> dict[str, dict]:
    return {slug: _scope_row(slug) for slug in slugs}


def test_register_inline_then_get(db_session):
    art = artifacts.register_artifact(
        db_session, body="# spec body", doc_kind="spec", maturity="draft"
    )
    assert art["backend"] == "inline"
    assert art["doc_kind"] == "spec"
    assert art["maturity"] == "draft"
    assert art["version_count"] == 1
    assert art["is_branched"] is False
    assert art["current_version"]["has_snapshot"] is True
    assert art["governs"] == [] and art["governs_all"] is False

    got = artifacts.get_artifact(db_session, art["id"])
    assert got["id"] == art["id"]
    assert got["current_version"]["id"] == art["current_version"]["id"]


def test_register_inline_requires_body(db_session):
    with pytest.raises(ValueError, match="needs a body"):
        artifacts.register_artifact(db_session, body=None)
    # An empty body IS valid content (a deliberately-empty doc).
    art = artifacts.register_artifact(db_session, body="")
    assert art["current_version"]["has_snapshot"] is True


def test_git_backend_rejected(db_session):
    with pytest.raises(artifacts.GitBackendUnsupportedError):
        artifacts.register_artifact(db_session, body="x", backend="git")


def test_register_validates_enums(db_session):
    with pytest.raises(ValueError, match="doc_kind"):
        artifacts.register_artifact(db_session, body="x", doc_kind="bogus")
    with pytest.raises(ValueError, match="maturity"):
        artifacts.register_artifact(db_session, body="x", maturity="bogus")


def test_revise_changes_leaf(db_session):
    art = artifacts.register_artifact(db_session, body="v1")
    v1_id = art["current_version"]["id"]
    revised = artifacts.revise_artifact(
        db_session, art["id"], relation="refines",
        body_snapshot="v2", summary="tightened",
    )
    assert revised["version_count"] == 2
    assert revised["is_branched"] is False
    # The current leaf moved off v1 onto the new version.
    assert revised["current_version"]["id"] != v1_id
    assert revised["current_version"]["summary"] == "tightened"
    assert revised["current_version"]["relation"] == "refines"


def test_revise_invalid_relation_raises(db_session):
    art = artifacts.register_artifact(db_session, body="v1")
    with pytest.raises(ValueError, match="relation"):
        artifacts.revise_artifact(db_session, art["id"], relation="bogus")


def test_resolve_competing_leaves(db_session):
    art = artifacts.register_artifact(db_session, body="root")
    root_v = art["current_version"]["id"]
    # Two versions superseding the same root → two competing leaves.
    a = artifacts.revise_artifact(
        db_session, art["id"], relation="refines",
        supersedes=root_v, body_snapshot="branch A",
    )
    branched = artifacts.revise_artifact(
        db_session, art["id"], relation="pivot",
        supersedes=root_v, body_snapshot="branch B",
    )
    assert branched["is_branched"] is True
    leaf_ids = {v["id"] for v in branched["leaves"]}
    assert len(leaf_ids) == 2

    # Resolve passes the LOSING leaf's version id (flipped to superseded); the
    # OTHER leaf remains canonical.
    loser = next(iter(leaf_ids))
    winner = (leaf_ids - {loser}).pop()
    resolved = artifacts.resolve_artifact(db_session, art["id"], loser)
    assert resolved["is_branched"] is False
    assert {v["id"] for v in resolved["leaves"]} == {winner}
    # `a` is referenced so its branch participates in the competing set.
    assert a["id"] == art["id"]


def test_resolve_requires_competing_leaves(db_session):
    art = artifacts.register_artifact(db_session, body="solo")
    with pytest.raises(ValueError, match="single current leaf"):
        artifacts.resolve_artifact(
            db_session, art["id"], art["current_version"]["id"]
        )


def test_set_maturity(db_session):
    art = artifacts.register_artifact(db_session, body="x", maturity="draft")
    out = artifacts.set_maturity(db_session, art["id"], "exploratory")
    assert out["maturity"] == "exploratory"
    # Maturity is a descriptor, not a gate — any direction allowed, no version.
    back = artifacts.set_maturity(db_session, art["id"], "draft")
    assert back["maturity"] == "draft"
    assert back["version_count"] == 1
    with pytest.raises(ValueError, match="maturity"):
        artifacts.set_maturity(db_session, art["id"], "bogus")


def test_set_governs_keys_on_scope_id(db_session):
    art = artifacts.register_artifact(db_session, body="x")
    out = artifacts.set_governs(
        db_session, art["id"], "acme/widget",
        resolved_scopes=_resolved("acme/widget"),
    )
    assert out["governs"] == ["acme/widget"]
    assert out["governs_all"] is False

    # `*` sets governs_all and clears the rows (mutually exclusive).
    star = artifacts.set_governs(db_session, art["id"], "*")
    assert star["governs"] == [] and star["governs_all"] is True

    # None clears both.
    cleared = artifacts.set_governs(db_session, art["id"], None)
    assert cleared["governs"] == [] and cleared["governs_all"] is False


def test_set_governs_unresolved_slug_raises(db_session):
    art = artifacts.register_artifact(db_session, body="x")
    with pytest.raises(ValueError, match="not resolved"):
        artifacts.set_governs(
            db_session, art["id"], "acme/unknown", resolved_scopes={}
        )


def test_register_with_governs_list(db_session):
    art = artifacts.register_artifact(
        db_session, body="x",
        governs=["acme/widget", "acme/other"],
        resolved_scopes=_resolved("acme/widget", "acme/other"),
    )
    assert art["governs"] == ["acme/other", "acme/widget"]  # sorted


def test_list_artifacts_filter_by_governs(db_session, stub_scope_client):
    a1 = artifacts.register_artifact(
        db_session, body="governs widget",
        governs="acme/widget", resolved_scopes=_resolved("acme/widget"),
    )
    artifacts.register_artifact(
        db_session, body="governs other",
        governs="acme/other", resolved_scopes=_resolved("acme/other"),
    )
    # A governs_all artifact surfaces under any per-scope filter too.
    a3 = artifacts.register_artifact(db_session, body="everywhere", governs="*")

    out = artifacts.list_artifacts(
        db_session, governs="acme/widget", governs_scope_id=_sid("acme/widget")
    )
    ids = {a["id"] for a in out["artifacts"]}
    assert a1["id"] in ids and a3["id"] in ids
    assert out["items_total"] == 2

    # An unresolved governs scope yields an empty list (not an error).
    empty = artifacts.list_artifacts(
        db_session, governs="acme/missing", governs_scope_id=None
    )
    assert empty["artifacts"] == [] and empty["items_total"] == 0


def test_applicable_artifacts_inherits_ancestors(db_session, stub_scope_client):
    """`applicable_artifacts` returns own + ancestor-governing artifacts via the
    stub scope client, tagging inherited rows with `from_scope` and keying the
    governs match on the STABLE scope_id."""
    org = artifacts.register_artifact(
        db_session, body="org reference", doc_kind="reference",
        governs="acme", resolved_scopes=_resolved("acme"),
    )
    repo = artifacts.register_artifact(
        db_session, body="repo spec",
        governs="acme/widget", resolved_scopes=_resolved("acme/widget"),
    )
    feat = artifacts.register_artifact(
        db_session, body="feature plan", doc_kind="plan",
        governs="acme/widget/feat", resolved_scopes=_resolved("acme/widget/feat"),
    )
    everywhere = artifacts.register_artifact(db_session, body="conventions", governs="*")

    stub = stub_scope_client(
        tree={
            "acme": None,
            "acme/widget": "acme",
            "acme/widget/feat": "acme/widget",
        }
    )
    out = artifacts.applicable_artifacts(db_session, "acme/widget/feat", stub)
    assert stub.ancestors_calls == ["acme/widget/feat"]

    by_id = {a["id"]: a for a in out["artifacts"]}
    assert set(by_id) == {org["id"], repo["id"], feat["id"], everywhere["id"]}
    # Own-scope match carries NO from_scope; inherited rows carry the ancestor.
    assert "from_scope" not in by_id[feat["id"]]
    assert by_id[repo["id"]]["from_scope"] == "acme/widget"
    assert by_id[org["id"]]["from_scope"] == "acme"
    # governs_all is tagged from_scope='*'.
    assert by_id[everywhere["id"]]["from_scope"] == "*"


def test_applicable_artifacts_halts_at_isolated(db_session, stub_scope_client):
    """An isolated middle scope blocks governs inheritance from ABOVE it."""
    artifacts.register_artifact(
        db_session, body="org", governs="acme", resolved_scopes=_resolved("acme")
    )
    repo = artifacts.register_artifact(
        db_session, body="repo",
        governs="acme/widget", resolved_scopes=_resolved("acme/widget"),
    )
    feat = artifacts.register_artifact(
        db_session, body="feat",
        governs="acme/widget/feat", resolved_scopes=_resolved("acme/widget/feat"),
    )
    stub = stub_scope_client(
        tree={
            "acme": None,
            "acme/widget": "acme",
            "acme/widget/feat": "acme/widget",
        },
        isolated={"acme/widget"},  # blocks inheritance from acme
    )
    out = artifacts.applicable_artifacts(db_session, "acme/widget/feat", stub)
    got = {a["id"] for a in out["artifacts"]}
    assert got == {feat["id"], repo["id"]}  # org artifact NOT inherited
