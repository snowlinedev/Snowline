"""Unit tests for the increment-2 milestone service surface (milestones.md §7 +
the dependency parts of §2/§4): the `merge` verb (alias tombstones, terminal-
target resolution keeping chains depth-1, state compatibility, dependency-edge
re-pointing with a re-run cycle guard), the `aliases` closure read, and the
dependency edge verbs (`add_dependency` / `remove_dependency` / `dependencies`,
cycle-guarded over the global edge set). These are the §10 acceptance criteria
that fall in this cut.
"""

import pytest

from snowline_platform import milestones, scopes


def _anchors(s):
    """turtlesedge (org) -> turtlesedge/turtletracks (repo); plus an `other` org
    for the cross-anchor cases."""
    scopes.create(s, slug="turtlesedge", name="TurtlesEdge", kind="org")
    scopes.create(
        s, slug="turtlesedge/turtletracks", name="TurtleTracks", kind="project"
    )
    scopes.create(s, slug="other", name="Other", kind="org")
    s.flush()


def _mk(s, name, anchor="turtlesedge/turtletracks"):
    m = milestones.create(s, anchor=anchor, name=name)
    s.flush()
    return f"{anchor}/{name}"


# --- merge (§7) -------------------------------------------------------------


def test_merge_reads_via_either_address_agree(db_session):
    """§10: `merge(a, b)` — resolution of `a` returns `b` with the alias noted;
    `get(a)` still returns the tombstone AS ITSELF; the closure lists `a`."""
    _anchors(db_session)
    a = _mk(db_session, "v1-launch")
    b = _mk(db_session, "launch")
    res = milestones.merge(db_session, a, b)
    assert res["target"] == b
    # The reminder points the caller at the agent-side consumer reads (§7).
    assert "list_artifact_versions" in res["reminder"]
    assert "milestone_status" in res["reminder"]

    # resolve(a) follows the alias to b, flagging the hop.
    row, via_alias = milestones.resolve_row(db_session, a)
    assert milestones.address_of(row) == b
    assert via_alias is True

    # get(a) returns the tombstone itself, its target noted.
    tomb = milestones.get(db_session, a)
    assert tomb.merged_into_id is not None
    assert milestones.address_of(tomb.merged_into) == b

    # aliases(b) closes over a; aliases(a) resolves first, same target + set.
    assert milestones.aliases(db_session, b) == {"target": b, "aliases": [a]}
    assert milestones.aliases(db_session, a) == {"target": b, "aliases": [a]}


def test_create_on_tombstoned_name_fails(db_session):
    """§10: `create` on the tombstoned name fails (the name is reserved forever)."""
    _anchors(db_session)
    a = _mk(db_session, "v1-launch")
    b = _mk(db_session, "launch")
    milestones.merge(db_session, a, b)
    with pytest.raises(milestones.MilestoneConflictError):
        milestones.create(
            db_session, anchor="turtlesedge/turtletracks", name="v1-launch"
        )


def test_merge_leaves_transition_log_untouched(db_session):
    """§10: no plugin rows are rewritten AND merge is not a lifecycle transition —
    the transition logs of both milestones are untouched by the merge."""
    _anchors(db_session)
    a = _mk(db_session, "a")
    b = _mk(db_session, "b")
    milestones.activate(db_session, a)
    milestones.activate(db_session, b)
    before_a = milestones.transitions(db_session, a)
    before_b = milestones.transitions(db_session, b)
    milestones.merge(db_session, a, b)  # both active — compatible
    # get(a) is the tombstone; its own log is unchanged (merge logged nothing).
    assert milestones.transitions(db_session, a) == before_a
    assert milestones.transitions(db_session, b) == before_b


def test_merge_state_compatibility(db_session):
    """§10/§7: legal iff from.status == into.status OR from.status == planned.
    cancelled→active and achieved→planned are rejected; planned→anything and
    same-status are allowed."""
    _anchors(db_session)

    # cancelled → active: rejected.
    c = _mk(db_session, "c")
    ca = _mk(db_session, "ca")
    milestones.cancel(db_session, c)
    milestones.activate(db_session, ca)
    with pytest.raises(milestones.MilestoneMergeError):
        milestones.merge(db_session, c, ca)
    assert milestones.get(db_session, c).merged_into_id is None  # no write

    # achieved → planned: rejected.
    ach = _mk(db_session, "ach")
    pl = _mk(db_session, "pl")
    milestones.activate(db_session, ach)
    milestones.achieve(db_session, ach)
    with pytest.raises(milestones.MilestoneMergeError):
        milestones.merge(db_session, ach, pl)

    # planned → achieved: allowed (from is planned).
    p2 = _mk(db_session, "p2")
    done = _mk(db_session, "done")
    milestones.activate(db_session, done)
    milestones.achieve(db_session, done)
    milestones.merge(db_session, p2, done)
    assert milestones.get(db_session, p2).merged_into_id is not None

    # active → active: allowed (statuses match).
    x = _mk(db_session, "x")
    y = _mk(db_session, "y")
    milestones.activate(db_session, x)
    milestones.activate(db_session, y)
    milestones.merge(db_session, x, y)
    assert milestones.get(db_session, x).merged_into_id is not None


def test_merge_into_self_and_cycle_rejected(db_session):
    """§7 cycle guard: a merge whose terminal target equals `from` is rejected —
    both the direct self-merge and the `merge(a,b)` then `merge(b,a)` case."""
    _anchors(db_session)
    a = _mk(db_session, "a")
    b = _mk(db_session, "b")
    with pytest.raises(milestones.MilestoneMergeError):
        milestones.merge(db_session, a, a)  # terminal target == from
    milestones.merge(db_session, a, b)  # a -> b
    with pytest.raises(milestones.MilestoneMergeError):
        # into=a resolves to terminal b, which == from → rejected.
        milestones.merge(db_session, b, a)


def test_chained_merge_collapses_to_depth_1(db_session):
    """§7: alias chains stay depth-1. Merging into a tombstone stores the TERMINAL
    target; and merging a milestone that already has an inbound tombstone re-points
    that tombstone too — so `resolve`'s single hop is always correct."""
    _anchors(db_session)
    a = _mk(db_session, "a")
    b = _mk(db_session, "b")
    c = _mk(db_session, "c")

    # Ordering 1: merge into a tombstone. b->c first, then a into b stores a->c.
    milestones.merge(db_session, b, c)  # b -> c
    milestones.merge(db_session, a, b)  # into=b resolves to terminal c
    assert (
        milestones.address_of(milestones.get(db_session, a).merged_into) == c
    )
    assert milestones.address_of(milestones.resolve(db_session, a)) == c
    # Closure over c holds both a and b.
    assert milestones.aliases(db_session, c) == {"target": c, "aliases": [a, b]}

    # Ordering 2: an inbound tombstone is re-pointed when its target is merged on.
    d = _mk(db_session, "d")
    e = _mk(db_session, "e")
    f = _mk(db_session, "f")
    milestones.merge(db_session, d, e)  # d -> e
    milestones.merge(db_session, e, f)  # e merged onward; d must re-point to f
    assert (
        milestones.address_of(milestones.get(db_session, d).merged_into) == f
    )
    assert milestones.address_of(milestones.resolve(db_session, d)) == f


def test_merge_cross_anchor_tombstone_stays_at_origin(db_session):
    """§7: cross-anchor merges are allowed; the tombstone stays at its original
    anchor, its name reserved there forever."""
    _anchors(db_session)
    org_addr = _mk(db_session, "v1-launch", anchor="turtlesedge")
    repo_addr = _mk(db_session, "v1-launch")  # different anchor, same name
    milestones.merge(db_session, org_addr, repo_addr)
    tomb = milestones.get(db_session, org_addr)  # still at its org anchor
    assert tomb.anchor.slug == "turtlesedge"
    assert milestones.address_of(tomb.merged_into) == repo_addr
    with pytest.raises(milestones.MilestoneConflictError):
        milestones.create(db_session, anchor="turtlesedge", name="v1-launch")


def test_merge_repoints_dependency_edges_both_directions(db_session):
    """§7: `from`'s dependency edges (both directions) re-point to `into`, dedup,
    and self-edges produced by the re-point are dropped."""
    _anchors(db_session)
    a = _mk(db_session, "a")
    b = _mk(db_session, "b")
    dep = _mk(db_session, "dep")
    rev = _mk(db_session, "rev")
    milestones.add_dependency(db_session, a, dep)  # a depends on dep
    milestones.add_dependency(db_session, rev, a)  # rev depends on a
    milestones.add_dependency(db_session, a, b)  # a depends on b (→ self-edge)
    milestones.merge(db_session, a, b)  # a into b
    deps_b = milestones.dependencies(db_session, b)
    assert {r["address"] for r in deps_b["depends_on"]} == {dep}  # a→b self dropped
    assert {r["address"] for r in deps_b["dependents"]} == {rev}


def test_merge_fails_whole_if_edge_union_would_cycle(db_session):
    """§7: the merge FAILS (no partial write) if re-pointing the edges would cycle
    the global DAG. a→b, b→c; merging c into a re-points b→c to b→a, and a→b + b→a
    cycles."""
    _anchors(db_session)
    a = _mk(db_session, "a")
    b = _mk(db_session, "b")
    c = _mk(db_session, "c")
    milestones.add_dependency(db_session, a, b)  # a → b
    milestones.add_dependency(db_session, b, c)  # b → c
    with pytest.raises(milestones.MilestoneMergeError):
        milestones.merge(db_session, c, a)
    # Nothing written: c is not a tombstone and the edges are intact.
    assert milestones.get(db_session, c).merged_into_id is None
    deps = milestones.dependencies(db_session, b)
    assert {r["address"] for r in deps["depends_on"]} == {c}


def test_suffix_colliding_names_are_reserved(db_session):
    """A milestone named after an address-suffix route (`aliases`, `transitions`,
    `dependencies`, the lifecycle verbs) would make its own address misroute to
    the suffix handler with a SHORTER address — so those names are reserved and
    can never exist."""
    _anchors(db_session)
    for reserved in sorted(milestones.RESERVED_NAMES):
        with pytest.raises(milestones.InvalidMilestoneNameError):
            milestones.create(
                db_session, anchor="turtlesedge/turtletracks", name=reserved
            )


# --- dependencies (§2/§4) ---------------------------------------------------


def test_add_dependency_self_edge_rejected(db_session):
    _anchors(db_session)
    a = _mk(db_session, "a")
    with pytest.raises(milestones.MilestoneDependencyError):
        milestones.add_dependency(db_session, a, a)


def test_add_dependency_direct_cycle_rejected(db_session):
    """§10: the A→B / B→A union cycles — the second edge is rejected."""
    _anchors(db_session)
    a = _mk(db_session, "a")
    b = _mk(db_session, "b")
    milestones.add_dependency(db_session, a, b)
    with pytest.raises(milestones.DependencyCycleError):
        milestones.add_dependency(db_session, b, a)


def test_add_dependency_transitive_cycle_rejected(db_session):
    """A→B, B→C, then C→A would cycle transitively — rejected over the global set."""
    _anchors(db_session)
    a = _mk(db_session, "a")
    b = _mk(db_session, "b")
    c = _mk(db_session, "c")
    milestones.add_dependency(db_session, a, b)
    milestones.add_dependency(db_session, b, c)
    with pytest.raises(milestones.DependencyCycleError):
        milestones.add_dependency(db_session, c, a)


def test_dependency_cross_anchor_allowed(db_session):
    """§2: cross-anchor edges are allowed — the anchor is not a fence."""
    _anchors(db_session)
    org = _mk(db_session, "foundation", anchor="turtlesedge")
    repo = _mk(db_session, "spanish-beta")
    milestones.add_dependency(db_session, repo, org)  # repo depends on org
    deps = milestones.dependencies(db_session, repo)
    assert {r["address"] for r in deps["depends_on"]} == {org}
    # The org milestone sees the repo one as a dependent (cross-anchor).
    org_deps = milestones.dependencies(db_session, org)
    assert {r["address"] for r in org_deps["dependents"]} == {repo}


def test_add_dependency_is_idempotent(db_session):
    """Chosen contract: a duplicate edge is idempotent (no-op success), not a
    conflict — replay/retry stays safe."""
    _anchors(db_session)
    a = _mk(db_session, "a")
    b = _mk(db_session, "b")
    milestones.add_dependency(db_session, a, b)
    milestones.add_dependency(db_session, a, b)  # no error
    deps = milestones.dependencies(db_session, a)
    assert [r["address"] for r in deps["depends_on"]] == [b]  # exactly one edge


def test_remove_dependency_idempotent(db_session):
    _anchors(db_session)
    a = _mk(db_session, "a")
    b = _mk(db_session, "b")
    milestones.add_dependency(db_session, a, b)
    milestones.remove_dependency(db_session, a, b)
    assert milestones.dependencies(db_session, a)["depends_on"] == []
    milestones.remove_dependency(db_session, a, b)  # absent edge → no-op success


def test_dependencies_read_shape_surfaces_cancelled(db_session):
    """§4: the read returns both directions with raw status — a dependency on a
    CANCELLED milestone must be visible (PM flags blocked_by_cancelled)."""
    _anchors(db_session)
    beta = _mk(db_session, "spanish-beta")
    foundation = _mk(db_session, "localization-foundation")
    ga = _mk(db_session, "spanish-ga")
    milestones.add_dependency(db_session, beta, foundation)  # beta → foundation
    milestones.add_dependency(db_session, ga, beta)  # ga → beta
    milestones.cancel(db_session, foundation)  # a cancelled dependency

    deps = milestones.dependencies(db_session, beta)
    assert deps["address"] == beta
    assert deps["depends_on"] == [
        {"address": foundation, "status": "cancelled"}
    ]
    assert deps["dependents"] == [{"address": ga, "status": "planned"}]


def test_dependencies_resolve_through_alias(db_session):
    """A dependency read/edit via a tombstoned address reports the live target's
    edges (both refs resolve through the alias)."""
    _anchors(db_session)
    a = _mk(db_session, "a")
    b = _mk(db_session, "b")
    dep = _mk(db_session, "dep")
    milestones.merge(db_session, a, b)  # a -> b
    # Adding a dependency on the tombstoned `a` lands on `b`.
    milestones.add_dependency(db_session, dep, a)
    via_a = milestones.dependencies(db_session, a)  # resolves through the alias
    via_b = milestones.dependencies(db_session, b)
    assert {r["address"] for r in via_a["dependents"]} == {dep}
    assert {r["address"] for r in via_b["dependents"]} == {dep}
