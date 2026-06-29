"""Decision-graph behavior — record/get/list, supersede/leaves, and the
ancestor-inherited `applicable_decisions` against a STUBBED scope client.

DB-backed (skips cleanly when Postgres is unavailable). The scope dependency is
always a stub — these unit tests never require a running platform.
"""

from __future__ import annotations

import uuid

import pytest

from snowline_governance import decisions

# A scope id for the writes (governance stores it as a soft reference). Tests
# that also exercise applicability use the stub's deterministic per-slug id so
# the slug match (not the id) drives the leaf query.
SID = uuid.uuid4()


def test_record_get_list(db_session):
    rec = decisions.record_decision(
        db_session, "acme/widget", SID, "use postgres", "it's solid"
    )
    assert rec["scope"] == "acme/widget"
    assert rec["supersedes"] is None

    got = decisions.get_decision(db_session, rec["id"])
    assert got["decision"] == "use postgres"
    assert got["rationale"] == "it's solid"
    assert got["scope"] == "acme/widget"
    assert got["superseded_by"] is None

    listed = decisions.list_decisions(db_session, "acme/widget")
    assert listed["items_total"] == 1
    assert listed["decisions"][0]["id"] == rec["id"]


def test_supersede_changes_leaves(db_session):
    v1 = decisions.record_decision(db_session, "acme/widget", SID, "v1")
    v2 = decisions.supersede_decision(db_session, v1["id"], "v2", "revised")
    assert v2["supersedes"] == v1["id"]

    # Default list returns only the new leaf.
    leaves = decisions.list_decisions(db_session, "acme/widget")
    leaf_ids = {d["id"] for d in leaves["decisions"]}
    assert leaf_ids == {v2["id"]}
    assert leaves["items_total"] == 1

    # The full chain shows both, with lineage markers.
    full = decisions.list_decisions(db_session, "acme/widget", include_superseded=True)
    by_id = {d["id"]: d for d in full["decisions"]}
    assert set(by_id) == {v1["id"], v2["id"]}
    assert by_id[v1["id"]]["superseded_by"] == v2["id"]
    assert by_id[v2["id"]]["supersedes"] == v1["id"]


def test_supersede_scope_mismatch_raises(db_session):
    v1 = decisions.record_decision(db_session, "acme/widget", SID, "v1")
    with pytest.raises(decisions.DecisionScopeMismatchError):
        decisions.supersede_decision(
            db_session, v1["id"], "v2", scope="acme/other"
        )


def test_branching_dag_two_leaves(db_session):
    """Two decisions superseding one prior → a fork; both successors are leaves."""
    root = decisions.record_decision(db_session, "acme/widget", SID, "root")
    a = decisions.supersede_decision(db_session, root["id"], "branch A")
    b = decisions.supersede_decision(db_session, root["id"], "branch B")
    leaves = decisions.list_decisions(db_session, "acme/widget")
    assert {d["id"] for d in leaves["decisions"]} == {a["id"], b["id"]}


def test_applicable_decisions_inherits_ancestors(db_session, stub_scope_client):
    """`applicable_decisions` returns own + ancestor leaves via the stub scope
    client, tagging inherited rows with `from_scope`, and asserts it queried the
    chain + merged."""
    # Record one decision at each level of acme > acme/widget > acme/widget/feat.
    decisions.record_decision(db_session, "acme", SID, "org policy")
    decisions.record_decision(db_session, "acme/widget", SID, "repo policy")
    decisions.record_decision(db_session, "acme/widget/feat", SID, "feature note")

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
    decisions.record_decision(db_session, "acme", SID, "org policy")
    decisions.record_decision(db_session, "acme/widget", SID, "repo policy")
    decisions.record_decision(db_session, "acme/widget/feat", SID, "feature note")

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
    decisions.record_decision(db_session, "acme", SID, "org v1")
    v1 = decisions.list_decisions(db_session, "acme", include_superseded=True)
    org_v1_id = v1["decisions"][0]["id"]
    decisions.supersede_decision(db_session, org_v1_id, "org v2")
    decisions.record_decision(db_session, "acme/widget", SID, "repo policy")

    stub = stub_scope_client(tree={"acme": None, "acme/widget": "acme"})
    out = decisions.applicable_decisions(db_session, "acme/widget", stub)
    got = {d["decision"] for d in out["decisions"]}
    assert got == {"repo policy", "org v2"}  # org v1 superseded, not surfaced
