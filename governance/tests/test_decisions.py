"""Decision-graph behavior — record/get/list, supersede/leaves, and the
ancestor-inherited `applicable_decisions` against a STUBBED scope client.

DB-backed (skips cleanly when Postgres is unavailable). The scope dependency is
always a stub — these unit tests never require a running platform.
"""

from __future__ import annotations

import uuid

import pytest

from snowline_governance import decisions


def _sid(slug: str) -> uuid.UUID:
    """The stable per-slug scope id — IDENTICAL to `StubScopeClient`'s, so a write
    keyed on this id matches the leaves the stub's `resolve`/`ancestors` rows carry
    (the queries key on `scope_id`, not the mutable slug)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


def test_record_get_list(db_session):
    rec = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "use postgres", "it's solid"
    )
    assert rec["scope"] == "acme/widget"
    assert rec["supersedes"] is None

    got = decisions.get_decision(db_session, rec["id"])
    assert got["decision"] == "use postgres"
    assert got["rationale"] == "it's solid"
    assert got["scope"] == "acme/widget"
    assert got["superseded_by"] is None

    listed = decisions.list_decisions(
        db_session, _sid("acme/widget"), "acme/widget"
    )
    assert listed["scope"] == "acme/widget"
    assert listed["items_total"] == 1
    assert listed["decisions"][0]["id"] == rec["id"]


def test_supersede_changes_leaves(db_session):
    v1 = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "v1"
    )
    v2 = decisions.supersede_decision(db_session, v1["id"], "v2", "revised")
    assert v2["supersedes"] == v1["id"]

    # Default list returns only the new leaf.
    leaves = decisions.list_decisions(
        db_session, _sid("acme/widget"), "acme/widget"
    )
    leaf_ids = {d["id"] for d in leaves["decisions"]}
    assert leaf_ids == {v2["id"]}
    assert leaves["items_total"] == 1

    # The full chain shows both, with lineage markers.
    full = decisions.list_decisions(
        db_session, _sid("acme/widget"), "acme/widget", include_superseded=True
    )
    by_id = {d["id"]: d for d in full["decisions"]}
    assert set(by_id) == {v1["id"], v2["id"]}
    assert by_id[v1["id"]]["superseded_by"] == v2["id"]
    assert by_id[v2["id"]]["supersedes"] == v1["id"]


def test_supersede_scope_mismatch_raises(db_session):
    v1 = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "v1"
    )
    with pytest.raises(decisions.DecisionScopeMismatchError):
        decisions.supersede_decision(
            db_session, v1["id"], "v2", scope="acme/other"
        )


def test_branching_dag_two_leaves(db_session):
    """Two decisions superseding one prior → a fork; both successors are leaves."""
    root = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "root"
    )
    a = decisions.supersede_decision(db_session, root["id"], "branch A")
    b = decisions.supersede_decision(db_session, root["id"], "branch B")
    leaves = decisions.list_decisions(
        db_session, _sid("acme/widget"), "acme/widget"
    )
    assert {d["id"] for d in leaves["decisions"]} == {a["id"], b["id"]}


def test_applicable_decisions_inherits_ancestors(db_session, stub_scope_client):
    """`applicable_decisions` returns own + ancestor leaves via the stub scope
    client, tagging inherited rows with `from_scope`, and asserts it queried the
    chain + merged."""
    # Record one decision at each level of acme > acme/widget > acme/widget/feat,
    # each keyed on THAT scope's own stable id (not a shared one).
    decisions.record_decision(db_session, "acme", _sid("acme"), "org policy")
    decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "repo policy"
    )
    decisions.record_decision(
        db_session, "acme/widget/feat", _sid("acme/widget/feat"), "feature note"
    )

    stub = stub_scope_client(
        tree={
            "acme": None,
            "acme/widget": "acme",
            "acme/widget/feat": "acme/widget",
        }
    )
    out = decisions.applicable_decisions(db_session, "acme/widget/feat", stub)

    # It asked the platform for the reader scope's ancestor chain.
    assert stub.ancestors_calls == ["acme/widget/feat"]

    by_decision = {d["decision"]: d for d in out["decisions"]}
    assert set(by_decision) == {"feature note", "repo policy", "org policy"}
    # Own-scope row carries NO from_scope; inherited rows carry the ancestor slug.
    assert "from_scope" not in by_decision["feature note"]
    assert by_decision["repo policy"]["from_scope"] == "acme/widget"
    assert by_decision["org policy"]["from_scope"] == "acme"
    # Own scope first in the walk order.
    assert out["decisions"][0]["decision"] == "feature note"


def test_applicable_decisions_halts_at_isolated(db_session, stub_scope_client):
    """An isolated middle scope blocks inheritance from ABOVE it: the isolated
    node's own decisions resolve, its parent's do not."""
    decisions.record_decision(db_session, "acme", _sid("acme"), "org policy")
    decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "repo policy"
    )
    decisions.record_decision(
        db_session, "acme/widget/feat", _sid("acme/widget/feat"), "feature note"
    )

    stub = stub_scope_client(
        tree={
            "acme": None,
            "acme/widget": "acme",
            "acme/widget/feat": "acme/widget",
        },
        isolated={"acme/widget"},  # blocks inheritance from acme
    )
    out = decisions.applicable_decisions(db_session, "acme/widget/feat", stub)
    got = {d["decision"] for d in out["decisions"]}
    assert got == {"feature note", "repo policy"}  # org policy is NOT inherited


def test_applicable_decisions_only_current_leaves(db_session, stub_scope_client):
    """Inherited decisions are filtered to current leaves per scope."""
    decisions.record_decision(db_session, "acme", _sid("acme"), "org v1")
    v1 = decisions.list_decisions(
        db_session, _sid("acme"), "acme", include_superseded=True
    )
    org_v1_id = v1["decisions"][0]["id"]
    decisions.supersede_decision(db_session, org_v1_id, "org v2")
    decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "repo policy"
    )

    stub = stub_scope_client(tree={"acme": None, "acme/widget": "acme"})
    out = decisions.applicable_decisions(db_session, "acme/widget", stub)
    got = {d["decision"] for d in out["decisions"]}
    assert got == {"repo policy", "org v2"}  # org v1 superseded, not surfaced


def test_decision_survives_slug_rename(db_session, stub_scope_client):
    """Rename-safety regression (#11): a decision recorded at a scope stays visible
    after a platform-side slug rename, because reads key on the STABLE `scope_id`,
    not the mutable slug.

    The rename is simulated by recording under the OLD slug, then resolving the NEW
    slug to the SAME stable id (`RenamingStubScopeClient` maps the new slug onto the
    old slug's id and serves the new slug in the ancestor chain). Pre-fix — when the
    queries keyed on `scope_slug` — both reads returned ZERO rows for the new slug.
    """
    old_slug = "acme/widget"
    new_slug = "acme/gadget"
    sid = _sid(old_slug)  # the id that survives the rename

    # A decision recorded BEFORE the rename keeps the old slug on its row.
    rec = decisions.record_decision(db_session, old_slug, sid, "use postgres")

    class RenamingStubScopeClient:
        """Stub where `new_slug` resolves to the SAME id the old slug had — the
        platform renamed the scope (id stable, slug changed) and now serves the new
        slug everywhere, including the ancestor chain."""

        def __init__(self) -> None:
            self.ancestors_calls: list[str] = []
            self.resolve_calls: list[str] = []

        def _row(self, slug: str) -> dict:
            return {
                "id": str(sid),  # SAME id regardless of which slug
                "slug": slug,
                "name": slug,
                "kind": "project",
                "status": "active",
                "isolated": False,
                "org": slug.split("/", 1)[0],
            }

        def resolve(self, slug: str) -> dict | None:
            self.resolve_calls.append(slug)
            return self._row(slug)

        def ancestors(self, slug: str) -> list[dict]:
            self.ancestors_calls.append(slug)
            return [self._row(slug)]  # root-only chain, serving the NEW slug

    stub = RenamingStubScopeClient()

    # applicable_decisions at the NEW slug still surfaces the pre-rename decision.
    app = decisions.applicable_decisions(db_session, new_slug, stub)
    assert {d["decision"] for d in app["decisions"]} == {"use postgres"}
    assert app["decisions"][0]["id"] == rec["id"]

    # list_decisions via the resolved (stable) id also still finds it; the response
    # `scope` reflects the NEW slug the platform now serves.
    sc = stub.resolve(new_slug)
    listed = decisions.list_decisions(db_session, sc["id"], sc["slug"])
    assert listed["scope"] == new_slug
    assert {d["id"] for d in listed["decisions"]} == {rec["id"]}
