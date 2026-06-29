"""Graduation behavior — the shadow → real crossing.

Graduating a `ShadowNode` mints a real `Decision` carrying the node's statement +
rationale, stamps bidirectional provenance (the decision's `shadow_origin_node_id`
+ `shadow_origin_label`, the node's `graduated_decision_id`), is idempotent, and
drags the cited-shadow-ancestor closure along in dependency order. Plus the
SURFACE assertion: `graduate` is a real-write verb on `main`, ABSENT from shadow
(the principal split, decision 99b92e1d) — see also test_shadow.py's isolation
test, which now includes `graduate` in `_REAL_WRITE_VERBS`.

DB-backed tests skip cleanly when Postgres is unavailable; the surface-shape
assertion needs no DB.
"""

from __future__ import annotations

import uuid

from snowline_governance import decisions, graduation, shadow
from snowline_governance.mcp_surface import build_main_surface, build_shadow_surface
from snowline_governance.models import Decision, ShadowNode


def _sid(slug: str) -> uuid.UUID:
    """The stable per-slug scope id — IDENTICAL to `StubScopeClient`'s."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


class _NoopScopeClient:
    def resolve(self, slug: str): return None
    def ancestors(self, slug: str): return []


# === graduation behavior =====================================================


def test_graduate_node_creates_real_decision_with_provenance(db_session):
    """Graduating a node mints a real `Decision` with the node's statement +
    rationale, stamps the decision's `shadow_origin_node_id` + label, and points
    the node's `graduated_decision_id` back at it."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "auth-as-jwt", "exploring")
    node = shadow.add_node(
        db_session, slug, "auth-as-jwt", "use rs256", "asymmetric keys"
    )

    out = graduation.graduate_node(db_session, node["id"])
    assert out["already_graduated"] is False
    assert out["scope"] == slug
    assert out["address"] == "acme/widget:auth-as-jwt"

    # A real decision exists, recorded at the cradle scope, carrying the node text.
    dec = db_session.get(Decision, uuid.UUID(out["decision_id"]))
    assert dec is not None
    assert dec.decision == "use rs256"
    assert dec.rationale == "asymmetric keys"
    assert dec.scope_id == _sid(slug)
    assert dec.scope_slug == slug
    # Bidirectional provenance: the decision's one-way backlink …
    assert dec.shadow_origin_node_id == node["id"]
    assert dec.shadow_origin_label == "acme/widget:auth-as-jwt"
    # … and the node's forward marker.
    node_row = db_session.get(ShadowNode, uuid.UUID(node["id"]))
    assert str(node_row.graduated_decision_id) == out["decision_id"]


def test_graduate_node_honours_ratified_edits(db_session):
    """The human-ratified `decision`/`rationale` override the node's text on the
    real decision (the node text is only the prefill)."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    node = shadow.add_node(db_session, slug, "line-a", "draft text", "draft why")

    out = graduation.graduate_node(
        db_session, node["id"], decision="final text", rationale="final why"
    )
    dec = db_session.get(Decision, uuid.UUID(out["decision_id"]))
    assert dec.decision == "final text"
    assert dec.rationale == "final why"


def test_graduate_node_is_idempotent(db_session):
    """Re-graduating an already-graduated node returns the EXISTING decision and
    creates nothing new."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    node = shadow.add_node(db_session, slug, "line-a", "the decision")

    first = graduation.graduate_node(db_session, node["id"])
    assert first["already_graduated"] is False

    before = db_session.query(Decision).count()
    second = graduation.graduate_node(db_session, node["id"])
    after = db_session.query(Decision).count()

    assert second["already_graduated"] is True
    assert second["decision_id"] == first["decision_id"]
    assert after == before  # no double-create


def test_graduate_node_at_explicit_scope(db_session):
    """An explicit destination scope (broader/org) records the real decision THERE
    rather than at the node's cradle scope — the soft scope ref the caller resolved
    is passed straight through."""
    cradle = "acme/widget"
    org = "acme"
    shadow.create_branch(db_session, cradle, _sid(cradle), "line-a")
    node = shadow.add_node(db_session, cradle, "line-a", "an org-wide call")

    out = graduation.graduate_node(
        db_session, node["id"],
        dest_scope_slug=org, dest_scope_id=_sid(org),
    )
    dec = db_session.get(Decision, uuid.UUID(out["decision_id"]))
    assert dec.scope_id == _sid(org)
    assert dec.scope_slug == org
    # Provenance label still points at the cradle branch (where it was speculated).
    assert dec.shadow_origin_label == "acme/widget:line-a"


def test_graduate_node_promotes_cited_ancestor_closure(db_session):
    """The cherry-pick closure (spec §6.4): graduating a node that cites an
    un-graduated shadow ancestor graduates the ancestor too, in dependency order,
    so the referenced reasoning comes along into the real graph."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    # n2 cites n1 (within-branch). Graduating n2 should also graduate n1.
    n1 = shadow.add_node(db_session, slug, "line-a", "premise", "the basis")
    n2 = shadow.add_node(db_session, slug, "line-a", "conclusion", "follows from premise")
    shadow.add_citation(db_session, n2["id"], cited_node_id=n1["id"])

    out = graduation.graduate_node(db_session, n2["id"])
    assert len(out["closure_promoted"]) == 1

    # n1 (the cited ancestor) is now graduated, as a real decision carrying ITS text.
    n1_row = db_session.get(ShadowNode, uuid.UUID(n1["id"]))
    assert n1_row.graduated_decision_id is not None
    anc_dec = db_session.get(Decision, n1_row.graduated_decision_id)
    assert anc_dec.decision == "premise"
    assert anc_dec.shadow_origin_node_id == n1["id"]

    # The promoted closure id matches the ancestor's graduated decision.
    assert out["closure_promoted"] == [str(n1_row.graduated_decision_id)]


def test_graduate_node_real_citation_not_dragged(db_session):
    """A node citing a REAL decision (already real) drags nothing into the closure —
    only un-graduated SHADOW ancestors are cherry-picked."""
    slug = "acme/widget"
    real = decisions.record_decision(db_session, slug, _sid(slug), "ground truth")
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    node = shadow.add_node(db_session, slug, "line-a", "builds on real")
    shadow.add_citation(db_session, node["id"], cited_decision_id=real["id"])

    out = graduation.graduate_node(db_session, node["id"])
    assert out["closure_promoted"] == []


def test_graduate_node_closure_off(db_session):
    """With `promote_closure=False` only the single node graduates; the cited
    shadow ancestor stays speculative."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    n1 = shadow.add_node(db_session, slug, "line-a", "premise")
    n2 = shadow.add_node(db_session, slug, "line-a", "conclusion")
    shadow.add_citation(db_session, n2["id"], cited_node_id=n1["id"])

    out = graduation.graduate_node(db_session, n2["id"], promote_closure=False)
    assert out["closure_promoted"] == []
    n1_row = db_session.get(ShadowNode, uuid.UUID(n1["id"]))
    assert n1_row.graduated_decision_id is None


# === surface placement — graduate is MAIN-only (the principal split) ==========


def test_graduate_is_on_main_absent_from_shadow():
    """`graduate` mints a REAL decision, so it lives on `main` and is ABSENT from
    the shadow surface (decision 99b92e1d): the shadow agent drafts, the principal
    on main executes. (The exhaustive isolation set is asserted in test_shadow.py;
    this is the direct graduate-specific contrapositive.)"""
    import anyio

    def _names(surface) -> set[str]:
        return {t.name for t in anyio.run(surface.list_tools)}

    main = _names(build_main_surface(scope_client=_NoopScopeClient()))
    sh = _names(build_shadow_surface(scope_client=_NoopScopeClient()))

    assert "graduate" in main
    assert "graduate" not in sh
