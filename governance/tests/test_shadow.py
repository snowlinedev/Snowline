"""Shadow / speculation graph behavior — branch/node/citation roundtrips, the
inward-only citation invariant, corpus search, and the KEY isolation property:
the `shadow` MCP surface carries the shadow writes + read-real grounding and ZERO
real-write verbs (that absence IS the isolation, decision 8a7f0a11).

DB-backed tests skip cleanly when Postgres is unavailable; the surface-shape
isolation test needs no DB (it enumerates `list_tools()`).
"""

from __future__ import annotations

import uuid

import anyio
import pytest

from snowline_governance import decisions, shadow
from snowline_governance.mcp_surface import build_main_surface, build_shadow_surface
from snowline_governance.models import Decision


def _sid(slug: str) -> uuid.UUID:
    """The stable per-slug scope id — IDENTICAL to `StubScopeClient`'s."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


class _NoopScopeClient:
    def resolve(self, slug: str): return None
    def ancestors(self, slug: str): return []


# === the shadow surface tool set (the isolation property) — no DB needed =====

# Every real-write verb that MUST be absent from the shadow surface (decision
# 8a7f0a11): a speculation session physically cannot mutate the real graph.
_REAL_WRITE_VERBS = {
    "record_decision",
    "supersede_decision",
    # graduation mints a REAL decision (shadow → real), so it's a real-write verb
    # that MUST live on `main` and be ABSENT from shadow (the principal split,
    # decision 99b92e1d): the shadow agent drafts, the principal on main executes.
    "graduate",
    "register_artifact",
    "revise_artifact",
    "resolve_artifact",
    "set_governs",
    "set_maturity",
}

_SHADOW_WRITE_TOOLS = {
    "create_branch",
    "list_branches",
    "get_branch",
    "set_narrative_notes",
    "add_node",
    "add_citation",
    "list_citations",
    "shadow_corpus_search",
}

_READ_REAL_GROUNDING = {
    "get_decision",
    "list_decisions",
    "applicable_decisions",
    "get_artifact",
    "list_artifacts",
    "applicable_artifacts",
}


def _tool_names(surface) -> set[str]:
    return {t.name for t in anyio.run(surface.list_tools)}


def test_shadow_surface_is_isolated_no_real_write():
    """THE KEY isolation test: the shadow surface's tool set is EXACTLY the 8
    shadow writes + the 6 read-real grounding tools — and contains ZERO real-write
    verbs. The absence of record_decision / supersede / register / revise /
    resolve / set_governs / set_maturity IS the isolation (decision 8a7f0a11)."""
    surface = build_shadow_surface(scope_client=_NoopScopeClient())
    tools = _tool_names(surface)

    # The shadow writes + read-real grounding are all present.
    assert _SHADOW_WRITE_TOOLS <= tools
    assert _READ_REAL_GROUNDING <= tools
    # And NOTHING else — the surface is exactly these two sets.
    assert tools == _SHADOW_WRITE_TOOLS | _READ_REAL_GROUNDING
    # The load-bearing assertion: every real-write verb is ABSENT.
    assert _REAL_WRITE_VERBS.isdisjoint(tools)
    for verb in _REAL_WRITE_VERBS:
        assert verb not in tools


def test_main_surface_keeps_the_real_write_verbs():
    """The contrapositive: the real-write verbs DO live on the `main` surface (so
    the isolation is a real split, not the tools simply not existing)."""
    surface = build_main_surface(scope_client=_NoopScopeClient())
    tools = _tool_names(surface)
    assert _REAL_WRITE_VERBS <= tools
    # The read-real grounding is shared (registered on both surfaces).
    assert _READ_REAL_GROUNDING <= tools
    # The shadow WRITE verbs are NOT on the main surface.
    assert _SHADOW_WRITE_TOOLS.isdisjoint(tools)


# === branch / node / citation roundtrips =====================================


def test_branch_roundtrip(db_session):
    slug = "acme/widget"
    created = shadow.create_branch(
        db_session, slug, _sid(slug), "auth-as-jwt", "exploring jwt"
    )
    assert created["scope"] == slug
    assert created["name"] == "auth-as-jwt"
    assert created["address"] == "acme/widget:auth-as-jwt"
    assert created["status"] == "active"
    assert created["nodes"] == []

    got = shadow.get_branch(db_session, slug, "auth-as-jwt")
    assert got["id"] == created["id"]
    assert got["narrative_notes"] == "exploring jwt"

    listed = shadow.list_branches(db_session, slug, _sid(slug))
    assert [b["id"] for b in listed] == [created["id"]]


def test_duplicate_branch_rejected(db_session):
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    with pytest.raises(shadow.DuplicateBranchError):
        shadow.create_branch(db_session, slug, _sid(slug), "line-a")


def test_set_narrative_notes(db_session):
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    updated = shadow.set_narrative_notes(
        db_session, slug, "line-a", "we decided to pivot"
    )
    assert updated["narrative_notes"] == "we decided to pivot"
    assert shadow.get_branch(db_session, slug, "line-a")["narrative_notes"] == (
        "we decided to pivot"
    )


def test_node_roundtrip(db_session):
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    node = shadow.add_node(
        db_session, slug, "line-a", "use rs256", "asymmetric keys"
    )
    assert node["statement"] == "use rs256"
    assert node["rationale"] == "asymmetric keys"
    assert node["graduated_decision_id"] is None

    branch = shadow.get_branch(db_session, slug, "line-a")
    assert [n["id"] for n in branch["nodes"]] == [node["id"]]


def test_branch_not_found(db_session):
    with pytest.raises(shadow.BranchNotFoundError):
        shadow.get_branch(db_session, "acme/widget", "nope")


# === the citation invariant (inward-only) ====================================


def test_citation_node_to_node_same_branch(db_session):
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    n1 = shadow.add_node(db_session, slug, "line-a", "decision 1")
    n2 = shadow.add_node(db_session, slug, "line-a", "decision 2")
    cit = shadow.add_citation(db_session, n2["id"], cited_node_id=n1["id"])
    assert cit["cited_node_id"] == n1["id"]
    assert cit["cited_decision_id"] is None
    assert [c["id"] for c in shadow.list_citations(db_session, n2["id"])] == [
        cit["id"]
    ]


def test_citation_node_to_real_decision(db_session):
    """A node may cite a REAL decision — the permitted inward reference. The real
    decision's id is stored as a plain value; existence is validated here."""
    slug = "acme/widget"
    real = decisions.record_decision(
        db_session, slug, _sid(slug), "use postgres", "solid"
    )
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    n1 = shadow.add_node(db_session, slug, "line-a", "build on top of pg")
    cit = shadow.add_citation(
        db_session, n1["id"], cited_decision_id=real["id"]
    )
    assert cit["cited_decision_id"] == real["id"]
    assert cit["cited_node_id"] is None


def test_citation_cross_branch_rejected(db_session):
    """A node may NOT cite a node in a DIFFERENT branch — that would couple rival
    lines that must stay separable."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    shadow.create_branch(db_session, slug, _sid(slug), "line-b")
    n_a = shadow.add_node(db_session, slug, "line-a", "a1")
    n_b = shadow.add_node(db_session, slug, "line-b", "b1")
    with pytest.raises(shadow.CitationTargetError):
        shadow.add_citation(db_session, n_a["id"], cited_node_id=n_b["id"])


def test_citation_to_nonexistent_real_decision_rejected(db_session):
    """Citing a real decision that does not exist raises — the inward target must
    exist (validated at the service layer, since there is no FK)."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    n1 = shadow.add_node(db_session, slug, "line-a", "n1")
    with pytest.raises(shadow.CitationTargetError):
        shadow.add_citation(
            db_session, n1["id"], cited_decision_id=str(uuid.uuid4())
        )


def test_citation_requires_exactly_one_target(db_session):
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    n1 = shadow.add_node(db_session, slug, "line-a", "n1")
    # Zero targets.
    with pytest.raises(shadow.CitationTargetError):
        shadow.add_citation(db_session, n1["id"])
    # Two targets.
    other = shadow.add_node(db_session, slug, "line-a", "n2")
    real = decisions.record_decision(db_session, slug, _sid(slug), "d")
    with pytest.raises(shadow.CitationTargetError):
        shadow.add_citation(
            db_session,
            n1["id"],
            cited_node_id=other["id"],
            cited_decision_id=real["id"],
        )


# === corpus search ============================================================


def test_corpus_search_finds_branch_node_and_decision(db_session):
    slug = "acme/widget"
    # A real decision backlinked to a shadow line (shadow_origin_label set) is
    # part of the corpus.
    real = decisions.record_decision(
        db_session, slug, _sid(slug), "adopt elephant database", "graduated"
    )
    real_row = db_session.get(Decision, uuid.UUID(real["id"]))
    real_row.shadow_origin_label = "acme/widget:elephant"
    db_session.flush()

    shadow.create_branch(
        db_session, slug, _sid(slug), "elephant", "explore elephant storage"
    )
    shadow.add_node(
        db_session, slug, "elephant", "store on elephant", "scales well"
    )

    res = shadow.corpus_search(db_session, "elephant")
    kinds = {r["kind"] for r in res["results"]}
    assert "shadow_branch" in kinds
    assert "shadow_node" in kinds
    assert "decision" in kinds
    assert res["results_total"] >= 3


def test_corpus_search_scope_narrowing(db_session):
    slug_a = "acme/widget"
    slug_b = "acme/gadget"
    shadow.create_branch(db_session, slug_a, _sid(slug_a), "zebra", "zebra notes")
    shadow.create_branch(db_session, slug_b, _sid(slug_b), "zebra2", "zebra notes")

    scoped = shadow.corpus_search(
        db_session, "zebra", scope_id=_sid(slug_a), scope_slug=slug_a
    )
    # Only the branch anchored at acme/widget matches the exact-scope narrowing.
    branch_results = [r for r in scoped["results"] if r["kind"] == "shadow_branch"]
    assert len(branch_results) == 1
    assert scoped["scope"] == slug_a


def test_corpus_search_rejects_blank_query(db_session):
    with pytest.raises(ValueError):
        shadow.corpus_search(db_session, "   ")
