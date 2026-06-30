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


# === branch-level graduation =================================================


def test_graduate_branch_records_end_decision_and_promotes_selected(db_session):
    """Branch graduation records a synthesized END decision (kind='graduation',
    origin = the branch ADDRESS, NULL node_id) AND promotes each SELECTED node to a
    real decision; un-selected nodes stay un-graduated."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "auth-line", "exploring")
    keep = shadow.add_node(db_session, slug, "auth-line", "use rs256", "asymmetric")
    keep2 = shadow.add_node(db_session, slug, "auth-line", "rotate keys", "hygiene")
    drop = shadow.add_node(db_session, slug, "auth-line", "a tangent", "dead end")

    out = graduation.graduate_branch(
        db_session,
        scope_slug=slug,
        name="auth-line",
        dest_scope_slug=slug,
        dest_scope_id=_sid(slug),
        end_statement="adopt asymmetric JWT auth",
        end_rationale="synthesized from the line",
        include_node_ids=[keep["id"], keep2["id"]],
    )
    assert out["already_graduated"] is False
    assert out["address"] == "acme/widget:auth-line"
    assert out["scope"] == slug
    assert len(out["promoted_node_ids"]) == 2

    # The END decision: branch-level marker (address origin, NO node_id, kind).
    end = db_session.get(Decision, uuid.UUID(out["end_decision_id"]))
    assert end.decision == "adopt asymmetric JWT auth"
    assert end.shadow_origin_label == "acme/widget:auth-line"
    assert end.shadow_origin_node_id is None
    assert end.shadow_origin_kind == "graduation"
    assert end.scope_id == _sid(slug)

    # The selected nodes became real, stamped graduated.
    for n in (keep, keep2):
        row = db_session.get(ShadowNode, uuid.UUID(n["id"]))
        assert row.graduated_decision_id is not None
    # The un-selected node stays speculative.
    drop_row = db_session.get(ShadowNode, uuid.UUID(drop["id"]))
    assert drop_row.graduated_decision_id is None


def test_graduate_branch_is_idempotent(db_session):
    """A second graduate_branch call returns the SAME end decision and writes no
    second competing one."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    node = shadow.add_node(db_session, slug, "line-a", "a node")

    first = graduation.graduate_branch(
        db_session, scope_slug=slug, name="line-a",
        dest_scope_slug=slug, dest_scope_id=_sid(slug),
        end_statement="the conclusion", end_rationale=None,
        include_node_ids=[node["id"]],
    )
    assert first["already_graduated"] is False

    before = db_session.query(Decision).count()
    second = graduation.graduate_branch(
        db_session, scope_slug=slug, name="line-a",
        dest_scope_slug=slug, dest_scope_id=_sid(slug),
        end_statement="a different conclusion", end_rationale=None,
        include_node_ids=[node["id"]],
    )
    after = db_session.query(Decision).count()

    assert second["already_graduated"] is True
    assert second["end_decision_id"] == first["end_decision_id"]
    assert after == before  # no second end decision, no re-promote


def test_graduate_branch_skips_malformed_node_id(db_session):
    """A malformed id in `include_node_ids` is skipped, not a 500."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    good = shadow.add_node(db_session, slug, "line-a", "good node")

    out = graduation.graduate_branch(
        db_session, scope_slug=slug, name="line-a",
        dest_scope_slug=slug, dest_scope_id=_sid(slug),
        end_statement="end", end_rationale=None,
        include_node_ids=["not-a-uuid", good["id"]],
    )
    assert out["promoted_node_ids"] == [
        str(db_session.get(ShadowNode, uuid.UUID(good["id"])).graduated_decision_id)
    ]


def test_graduate_branch_at_explicit_dest_scope(db_session):
    """An explicit `dest_scope_*` (broader/org) lands the end + node decisions
    THERE, while the provenance address still names the cradle branch."""
    cradle = "acme/widget"
    org = "acme"
    shadow.create_branch(db_session, cradle, _sid(cradle), "line-a")
    node = shadow.add_node(db_session, cradle, "line-a", "org-wide call")

    out = graduation.graduate_branch(
        db_session, scope_slug=cradle, name="line-a",
        dest_scope_slug=org, dest_scope_id=_sid(org),
        end_statement="org decision", end_rationale=None,
        include_node_ids=[node["id"]],
    )
    end = db_session.get(Decision, uuid.UUID(out["end_decision_id"]))
    assert end.scope_id == _sid(org)
    assert end.shadow_origin_label == "acme/widget:line-a"  # cradle address
    promoted = db_session.get(Decision, uuid.UUID(out["promoted_node_ids"][0]))
    assert promoted.scope_id == _sid(org)


# === branch-level rejection ==================================================


def test_record_branch_rejection_in_cradle_scope(db_session):
    """A rejection records a REAL decision in the branch's CRADLE scope, kind=
    'rejection', address origin, NULL node_id."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "rejected-line", "nope")

    out = graduation.record_branch_rejection(
        db_session, scope_slug=slug, scope_id=_sid(slug), name="rejected-line",
        statement="we will NOT pursue this", rationale="too costly",
    )
    assert out["already_recorded"] is False
    assert out["address"] == "acme/widget:rejected-line"

    dec = db_session.get(Decision, uuid.UUID(out["rejection_decision_id"]))
    assert dec.decision == "we will NOT pursue this"
    assert dec.scope_id == _sid(slug)  # cradle scope
    assert dec.shadow_origin_label == "acme/widget:rejected-line"
    assert dec.shadow_origin_node_id is None
    assert dec.shadow_origin_kind == "rejection"


def test_record_branch_rejection_is_idempotent(db_session):
    """A second rejection on the same branch returns the existing one."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")

    first = graduation.record_branch_rejection(
        db_session, scope_slug=slug, scope_id=_sid(slug), name="line-a",
        statement="rejected", rationale=None,
    )
    before = db_session.query(Decision).count()
    second = graduation.record_branch_rejection(
        db_session, scope_slug=slug, scope_id=_sid(slug), name="line-a",
        statement="rejected differently", rationale=None,
    )
    after = db_session.query(Decision).count()

    assert second["already_recorded"] is True
    assert second["rejection_decision_id"] == first["rejection_decision_id"]
    assert after == before


def test_graduation_and_rejection_kinds_stay_distinct(db_session):
    """The `kind` filter keeps a graduation end decision and a rejection decision on
    the SAME branch address from being mistaken for each other (be803a2b)."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")

    grad = graduation.graduate_branch(
        db_session, scope_slug=slug, name="line-a",
        dest_scope_slug=slug, dest_scope_id=_sid(slug),
        end_statement="graduated end", end_rationale=None, include_node_ids=[],
    )
    rej = graduation.record_branch_rejection(
        db_session, scope_slug=slug, scope_id=_sid(slug), name="line-a",
        statement="and also a rejection facet", rationale=None,
    )
    # Two DISTINCT decisions on the same address — neither idempotency lookup
    # collapses onto the other.
    assert grad["end_decision_id"] != rej["rejection_decision_id"]
    assert grad["already_graduated"] is False
    assert rej["already_recorded"] is False

    # Re-running each returns its OWN prior decision, not the other.
    grad2 = graduation.graduate_branch(
        db_session, scope_slug=slug, name="line-a",
        dest_scope_slug=slug, dest_scope_id=_sid(slug),
        end_statement="x", end_rationale=None, include_node_ids=[],
    )
    rej2 = graduation.record_branch_rejection(
        db_session, scope_slug=slug, scope_id=_sid(slug), name="line-a",
        statement="y", rationale=None,
    )
    assert grad2["end_decision_id"] == grad["end_decision_id"]
    assert rej2["rejection_decision_id"] == rej["rejection_decision_id"]


# === archive_branch (pure shadow status flip) ================================


def test_archive_branch_flips_status_and_is_idempotent(db_session):
    """archive_branch flips active→archived and sets archived_at; a re-archive is a
    no-op that pins the original archived_at."""
    slug = "acme/widget"
    created = shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    assert created["status"] == "active"
    assert created["archived_at"] is None

    out = shadow.archive_branch(db_session, slug, "line-a")
    assert out["status"] == "archived"
    assert out["archived_at"] is not None
    first_archived_at = out["archived_at"]

    # Hidden from the default list, visible with include_done.
    assert shadow.list_branches(db_session, slug, _sid(slug)) == []
    listed = shadow.list_branches(db_session, slug, _sid(slug), include_done=True)
    assert [b["id"] for b in listed] == [created["id"]]

    # Idempotent: re-archive keeps the SAME archived_at.
    again = shadow.archive_branch(db_session, slug, "line-a")
    assert again["status"] == "archived"
    assert again["archived_at"] == first_archived_at


def test_reject_then_archive_flow(db_session):
    """The full §7 flow: record WHY (real rejection on main) THEN archive the line
    (pure shadow). The two are separable facets of shelving a speculation."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a", "explored")

    rej = graduation.record_branch_rejection(
        db_session, scope_slug=slug, scope_id=_sid(slug), name="line-a",
        statement="chose against it", rationale="better path found",
    )
    archived = shadow.archive_branch(db_session, slug, "line-a")

    # The rejection decision survives (the reasoning is kept) …
    assert db_session.get(
        Decision, uuid.UUID(rej["rejection_decision_id"])
    ) is not None
    # … and the line is shelved.
    assert archived["status"] == "archived"


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


def test_branch_graduation_and_rejection_are_main_only():
    """`graduate_branch` + `record_branch_rejection` mint real decisions → on
    `main`, ABSENT from shadow; `archive_branch` is a pure shadow op → on shadow,
    ABSENT from main (the principal split, 99b92e1d)."""
    import anyio

    def _names(surface) -> set[str]:
        return {t.name for t in anyio.run(surface.list_tools)}

    main = _names(build_main_surface(scope_client=_NoopScopeClient()))
    sh = _names(build_shadow_surface(scope_client=_NoopScopeClient()))

    # Real writes — main only.
    for verb in ("graduate_branch", "record_branch_rejection"):
        assert verb in main
        assert verb not in sh
    # Pure shadow status flip — shadow only.
    assert "archive_branch" in sh
    assert "archive_branch" not in main
