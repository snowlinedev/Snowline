"""Unit tests for the scope service — resolve, ancestors (isolation-halting),
tree (nesting + isolated + ordering), create (bare-slug⇔org + parent derivation).
"""

import pytest

from snowline_platform import scopes


def _build_tree(s):
    """org -> repo(ISOLATED middle) -> initiative -> reader. A multi-level tree
    with an isolated MIDDLE node, per the spec's acceptance criterion."""
    scopes.create(s, slug="org", name="Org", kind="org")
    scopes.create(s, slug="org/repo", name="Repo", kind="project", isolated=True)
    scopes.create(s, slug="org/repo/init", name="Init", kind="initiative")
    scopes.create(s, slug="org/repo/init/reader", name="Reader", kind="component")
    s.flush()


def test_resolve_found_and_none(db_session):
    scopes.create(db_session, slug="acme", name="Acme", kind="org")
    db_session.flush()
    assert scopes.resolve(db_session, "acme").slug == "acme"
    assert scopes.resolve(db_session, "nope") is None


def test_slug_input_is_case_insensitive(db_session):
    """#134: mixed-case GitHub-style input canonicalizes to lowercase at every
    seam — resolve, create (slug + parent), list org filter. Storage stays
    canonical lowercase."""
    scopes.create(db_session, slug="TurtlesEdge", name="TE", kind="org")
    created = scopes.create(
        db_session,
        slug="TurtlesEdge/TurtleTracks",
        name="TT",
        kind="project",
        parent="TURTLESEDGE",
    )
    db_session.flush()
    # Stored canonical; parent resolved despite casing.
    assert created.slug == "turtlesedge/turtletracks"
    assert created.parent_id is not None
    # Mixed-case resolve finds the canonical row.
    assert (
        scopes.resolve(db_session, "TurtlesEdge/turtletracks").slug
        == "turtlesedge/turtletracks"
    )
    # Mixed-case org filter narrows to the canonical org.
    rows = scopes.list_scopes(db_session, org="TurtlesEdge")
    assert {r["slug"] for r in rows} == {
        "turtlesedge",
        "turtlesedge/turtletracks",
    }
    # A duplicate differing only by case is the SAME scope → conflict.
    with pytest.raises(scopes.ScopeConflictError):
        scopes.create(
            db_session, slug="TURTLESEDGE/turtletracks", name="dup", kind="project"
        )


def test_ancestors_halt_at_isolated_middle_node(db_session):
    _build_tree(db_session)
    reader = scopes.resolve(db_session, "org/repo/init/reader")
    chain = [sc.slug for sc in scopes.ancestors(db_session, reader)]
    # own first, climb parent_id, STOP at the first isolated node (org/repo),
    # which is included; its parent (org) is NOT reached.
    assert chain == ["org/repo/init/reader", "org/repo/init", "org/repo"]
    assert "org" not in chain


def test_ancestors_isolated_node_itself_included_parent_excluded(db_session):
    _build_tree(db_session)
    repo = scopes.resolve(db_session, "org/repo")  # the isolated node itself
    chain = [sc.slug for sc in scopes.ancestors(db_session, repo)]
    assert chain == ["org/repo"]  # included; its parent org is not reached


def test_tree_nesting_ordering_and_isolated_exposed(db_session):
    _build_tree(db_session)
    forest = scopes.tree(db_session)
    assert len(forest) == 1
    org = forest[0]
    assert org["slug"] == "org"
    assert org["isolated"] is False
    repo = org["children"][0]
    assert repo["slug"] == "org/repo"
    assert repo["isolated"] is True  # exposed on every node
    init = repo["children"][0]
    assert init["slug"] == "org/repo/init"
    assert init["children"][0]["slug"] == "org/repo/init/reader"


def test_tree_root_subtree(db_session):
    _build_tree(db_session)
    sub = scopes.tree(db_session, root="org/repo/init")
    assert len(sub) == 1
    assert sub[0]["slug"] == "org/repo/init"
    assert sub[0]["children"][0]["slug"] == "org/repo/init/reader"


def test_tree_dangling_parent_is_forest_root(db_session):
    # A scope whose parent_id is None (no registered parent prefix) is a root,
    # so nothing is dropped.
    scopes.create(db_session, slug="lonely", name="Lonely", kind="org")
    scopes.create(db_session, slug="lonely/child", name="Child", kind="project")
    db_session.flush()
    forest = scopes.tree(db_session)
    assert {n["slug"] for n in forest} == {"lonely"}
    assert forest[0]["children"][0]["slug"] == "lonely/child"


def test_create_enforces_bare_slug_org_invariant(db_session):
    # A bare slug must be kind 'org'.
    with pytest.raises(scopes.InvalidScopeFieldError):
        scopes.create(db_session, slug="bare", name="x", kind="project")
    # 'org' is invalid for a hierarchical slug.
    scopes.create(db_session, slug="org", name="Org", kind="org")
    db_session.flush()
    with pytest.raises(scopes.InvalidScopeFieldError):
        scopes.create(db_session, slug="org/sub", name="x", kind="org")


def test_create_derives_parent_from_slug_hierarchy(db_session):
    org = scopes.create(db_session, slug="org", name="Org", kind="org")
    child = scopes.create(
        db_session, slug="org/repo", name="Repo", kind="project"
    )
    db_session.flush()
    assert child.parent_id == org.id  # derived from the slug prefix


def test_create_explicit_none_parent_skips_derivation(db_session):
    """`parent` NOT PROVIDED (the default) derives from the slug prefix;
    explicit `None` means no parent at all, even when a same-prefix scope
    exists — the distinction `apply_scope_event` relies on (a replica must
    replay the origin's OWN resolved `parent_id`, never silently attach an
    unrelated local scope it happens to hold under the same prefix)."""
    scopes.create(db_session, slug="org", name="Org", kind="org")
    child = scopes.create(
        db_session, slug="org/repo", name="Repo", kind="project", parent=None
    )
    db_session.flush()
    assert child.parent_id is None


def test_create_explicit_parent_must_exist(db_session):
    with pytest.raises(scopes.ScopeNotFoundError):
        scopes.create(
            db_session, slug="org/repo", name="r", kind="project",
            parent="ghost",
        )


def test_create_duplicate_conflicts(db_session):
    scopes.create(db_session, slug="org", name="Org", kind="org")
    db_session.flush()
    with pytest.raises(scopes.ScopeConflictError):
        scopes.create(db_session, slug="org", name="Org", kind="org")


def test_create_converts_flush_race_into_scope_conflict(db_session, monkeypatch):
    """The `resolve()` pre-check is check-then-act, not atomic (§8 replication
    review finding): two concurrent creates for the SAME slug (a local create
    racing an incoming replicated one) can both pass it before either commits.
    Simulate the race by blinding the pre-check to the winner already sitting
    in the session, then assert the DB's unique constraint still surfaces the
    SAME `ScopeConflictError` at flush — not a raw `IntegrityError` that would
    bypass `apply_scope_event`'s `ParkNow` fast path (#92)."""
    scopes.create(db_session, slug="org", name="Org", kind="org")
    db_session.flush()
    monkeypatch.setattr(scopes, "resolve", lambda session, slug: None)
    with pytest.raises(scopes.ScopeConflictError):
        scopes.create(db_session, slug="org", name="Org 2", kind="org")
    monkeypatch.undo()
    # The failed flush must not leave the session in a broken transaction —
    # ordinary work on it afterward should still succeed.
    scopes.create(db_session, slug="other", name="Other", kind="org")
    db_session.flush()
    assert scopes.resolve(db_session, "other") is not None


def test_create_org_rejects_parent(db_session):
    with pytest.raises(scopes.InvalidScopeFieldError):
        scopes.create(
            db_session, slug="org", name="Org", kind="org", parent="other"
        )


def test_list_scopes_org_filter(db_session):
    scopes.create(db_session, slug="a", name="A", kind="org")
    scopes.create(db_session, slug="a/r", name="AR", kind="project")
    scopes.create(db_session, slug="b", name="B", kind="org")
    db_session.flush()
    rows = scopes.list_scopes(db_session, org="a")
    assert {r["slug"] for r in rows} == {"a", "a/r"}
    assert all(r["org"] == "a" for r in rows)
