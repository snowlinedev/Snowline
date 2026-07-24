"""Unit tests for the milestone service (milestones.md §3/§4) — the increment-1
acceptance criteria (§10) that fall in this cut: resolution (direct, bare-name
walk with repo shadowing org, initiative-context normalization, no-context
hard-fail even with a unique candidate, typo hard-fail with suggestions and
nothing minted, mixed-case → canonical), create (anchor/name validation +
duplicate-by-case rejection), the full lifecycle legality table incl.
achieve-on-planned rejection, transition-log recording, and list filters.
"""

import pytest

from snowline_platform import milestones, scopes


def _anchors(s):
    """turtlesedge (org) -> turtlesedge/turtletracks (repo) -> .../i18n (init)."""
    scopes.create(s, slug="turtlesedge", name="TurtlesEdge", kind="org")
    scopes.create(
        s, slug="turtlesedge/turtletracks", name="TurtleTracks", kind="project"
    )
    scopes.create(
        s, slug="turtlesedge/turtletracks/i18n", name="i18n", kind="initiative"
    )
    s.flush()


# --- create -----------------------------------------------------------------


def test_create_and_get(db_session):
    _anchors(db_session)
    m = milestones.create(
        db_session,
        anchor="turtlesedge/turtletracks",
        name="spanish-beta",
        outcome="Spanish beta ships",
    )
    db_session.flush()
    assert m.status == "planned"
    assert milestones.address_of(m) == "turtlesedge/turtletracks/spanish-beta"
    got = milestones.get(db_session, "turtlesedge/turtletracks/spanish-beta")
    assert got.id == m.id


def test_create_rejects_non_anchor_segment_count(db_session):
    _anchors(db_session)
    # A 3-segment (initiative-level) anchor is not a legal milestone anchor.
    with pytest.raises(milestones.InvalidAnchorError):
        milestones.create(
            db_session, anchor="turtlesedge/turtletracks/i18n", name="x"
        )


def test_create_rejects_unregistered_anchor(db_session):
    _anchors(db_session)
    with pytest.raises(milestones.InvalidAnchorError):
        milestones.create(db_session, anchor="ghostorg/ghostrepo", name="x")


def test_create_rejects_slashed_name(db_session):
    _anchors(db_session)
    with pytest.raises(milestones.InvalidMilestoneNameError):
        milestones.create(
            db_session, anchor="turtlesedge/turtletracks", name="a/b"
        )


def test_create_duplicate_by_case_rejected(db_session):
    """§10: `create` differing only by case from an existing name fails as a
    duplicate (input folds to canonical before the uniqueness check)."""
    _anchors(db_session)
    milestones.create(
        db_session, anchor="turtlesedge/turtletracks", name="v1-launch"
    )
    db_session.flush()
    with pytest.raises(milestones.MilestoneConflictError):
        milestones.create(
            db_session, anchor="TurtlesEdge/TurtleTracks", name="V1-Launch"
        )


# --- resolution (§3) --------------------------------------------------------


def test_resolve_direct_two_and_three_segment(db_session):
    _anchors(db_session)
    milestones.create(db_session, anchor="turtlesedge", name="org-goal")
    milestones.create(
        db_session, anchor="turtlesedge/turtletracks", name="spanish-beta"
    )
    db_session.flush()
    assert (
        milestones.resolve(db_session, "turtlesedge/org-goal").name == "org-goal"
    )
    assert (
        milestones.resolve(
            db_session, "turtlesedge/turtletracks/spanish-beta"
        ).name
        == "spanish-beta"
    )


def test_bare_name_walk_repo_shadows_org(db_session):
    """§10: with both `org/x` and `org/repo/x` registered, a bare `x` in repo
    context resolves to the REPO anchor (repo shadows org, predictably)."""
    _anchors(db_session)
    org_m = milestones.create(db_session, anchor="turtlesedge", name="ga")
    repo_m = milestones.create(
        db_session, anchor="turtlesedge/turtletracks", name="ga"
    )
    db_session.flush()
    resolved = milestones.resolve(
        db_session, "ga", context="turtlesedge/turtletracks"
    )
    assert resolved.id == repo_m.id
    assert resolved.id != org_m.id
    # And in ORG context, the bare name resolves to the org anchor (no repo step).
    assert milestones.resolve(db_session, "ga", context="turtlesedge").id == org_m.id


def test_bare_name_initiative_context_normalizes_to_repo(db_session):
    """§10: an initiative-scoped context resolves via its repo — the context
    normalizes UP to its nearest repo-level ancestor before the walk."""
    _anchors(db_session)
    repo_m = milestones.create(
        db_session, anchor="turtlesedge/turtletracks", name="spanish-beta"
    )
    db_session.flush()
    resolved = milestones.resolve(
        db_session,
        "spanish-beta",
        context="turtlesedge/turtletracks/i18n",
    )
    assert resolved.id == repo_m.id


def test_bare_name_falls_through_repo_to_org(db_session):
    _anchors(db_session)
    org_m = milestones.create(db_session, anchor="turtlesedge", name="org-only")
    db_session.flush()
    # No repo-anchored `org-only`; the walk falls through repo to the org anchor.
    resolved = milestones.resolve(
        db_session, "org-only", context="turtlesedge/turtletracks"
    )
    assert resolved.id == org_m.id


def test_bare_name_no_context_hard_fails_even_when_unique(db_session):
    """§10: a bare name with NO context hard-fails even with a UNIQUE candidate
    (surfaced as a suggestion, never resolved) — the uniform-strictness rule."""
    _anchors(db_session)
    milestones.create(
        db_session, anchor="turtlesedge/turtletracks", name="only-one"
    )
    db_session.flush()
    with pytest.raises(milestones.MilestoneResolutionError) as exc:
        milestones.resolve(db_session, "only-one")  # no context
    # The unique candidate is offered as a suggestion, not auto-resolved.
    assert any(
        s["address"] == "turtlesedge/turtletracks/only-one"
        for s in exc.value.suggestions
    )


def test_bare_name_walk_miss_suggests_other_anchor_not_resolves(db_session):
    """A bare name never resolves outside the walk: a same-named milestone at a
    DIFFERENT anchor is a suggestion only (§3)."""
    _anchors(db_session)
    scopes.create(db_session, slug="otherorg", name="Other", kind="org")
    milestones.create(db_session, anchor="otherorg", name="elsewhere")
    db_session.flush()
    with pytest.raises(milestones.MilestoneResolutionError) as exc:
        milestones.resolve(
            db_session, "elsewhere", context="turtlesedge/turtletracks"
        )
    assert any(
        s["address"] == "otherorg/elsewhere" for s in exc.value.suggestions
    )


def test_typo_hard_fails_with_suggestion_and_nothing_minted(db_session):
    """§10: a typo'd ref hard-fails with a near-miss suggestion and NOTHING is
    minted (no auto-vivify at any surface)."""
    _anchors(db_session)
    milestones.create(
        db_session, anchor="turtlesedge/turtletracks", name="v1-launch"
    )
    db_session.flush()
    before = milestones.list_milestones(db_session)
    with pytest.raises(milestones.MilestoneResolutionError) as exc:
        milestones.resolve(db_session, "turtlesedge/turtletracks/v1-lanch")
    assert any(
        s["address"] == "turtlesedge/turtletracks/v1-launch"
        for s in exc.value.suggestions
    )
    # Nothing minted.
    after = milestones.list_milestones(db_session)
    assert len(after) == len(before) == 1


def test_mixed_case_ref_resolves_to_canonical(db_session):
    """§10: a mixed-case ref resolves to the canonical lowercase address."""
    _anchors(db_session)
    milestones.create(
        db_session, anchor="turtlesedge/turtletracks", name="v1-launch"
    )
    db_session.flush()
    m = milestones.resolve(
        db_session, "TurtlesEdge/turtletracks/V1-Launch"
    )
    assert milestones.address_of(m) == "turtlesedge/turtletracks/v1-launch"


def test_resolve_row_reports_no_alias_this_increment(db_session):
    _anchors(db_session)
    milestones.create(
        db_session, anchor="turtlesedge/turtletracks", name="spanish-beta"
    )
    db_session.flush()
    _, via_alias = milestones.resolve_row(
        db_session, "turtlesedge/turtletracks/spanish-beta"
    )
    assert via_alias is False


# --- lifecycle (§4) ---------------------------------------------------------


def test_full_lifecycle_planned_active_achieved(db_session):
    _anchors(db_session)
    addr = "turtlesedge/turtletracks/spanish-beta"
    milestones.create(db_session, anchor="turtlesedge/turtletracks", name="spanish-beta")
    db_session.flush()
    milestones.activate(db_session, addr, reason="kickoff")
    m = milestones.get(db_session, addr)
    assert m.status == "active" and m.activated_at is not None
    milestones.achieve(db_session, addr, reason="shipped")
    m = milestones.get(db_session, addr)
    assert m.status == "achieved" and m.achieved_at is not None


def test_achieve_on_planned_is_rejected(db_session):
    """§10: `achieve` on a planned milestone is rejected ("activate first") —
    achievement is never automatic."""
    _anchors(db_session)
    addr = "turtlesedge/turtletracks/spanish-beta"
    milestones.create(db_session, anchor="turtlesedge/turtletracks", name="spanish-beta")
    db_session.flush()
    with pytest.raises(milestones.IllegalTransitionError):
        milestones.achieve(db_session, addr)
    # Still planned — nothing changed.
    assert milestones.get(db_session, addr).status == "planned"


def test_cancel_from_planned_and_from_active(db_session):
    _anchors(db_session)
    milestones.create(db_session, anchor="turtlesedge/turtletracks", name="a")
    milestones.create(db_session, anchor="turtlesedge/turtletracks", name="b")
    db_session.flush()
    milestones.cancel(db_session, "turtlesedge/turtletracks/a")
    assert milestones.get(db_session, "turtlesedge/turtletracks/a").status == "cancelled"
    milestones.activate(db_session, "turtlesedge/turtletracks/b")
    milestones.cancel(db_session, "turtlesedge/turtletracks/b", reason="scrapped")
    m = milestones.get(db_session, "turtlesedge/turtletracks/b")
    assert m.status == "cancelled" and m.cancelled_at is not None


def test_illegal_transitions_from_terminal(db_session):
    _anchors(db_session)
    addr = "turtlesedge/turtletracks/done"
    milestones.create(db_session, anchor="turtlesedge/turtletracks", name="done")
    db_session.flush()
    milestones.activate(db_session, addr)
    milestones.achieve(db_session, addr)
    # No transition out of a terminal status.
    with pytest.raises(milestones.IllegalTransitionError):
        milestones.activate(db_session, addr)
    with pytest.raises(milestones.IllegalTransitionError):
        milestones.cancel(db_session, addr)
    with pytest.raises(milestones.IllegalTransitionError):
        milestones.achieve(db_session, addr)


def test_double_activate_rejected(db_session):
    _anchors(db_session)
    addr = "turtlesedge/turtletracks/x"
    milestones.create(db_session, anchor="turtlesedge/turtletracks", name="x")
    db_session.flush()
    milestones.activate(db_session, addr)
    with pytest.raises(milestones.IllegalTransitionError):
        milestones.activate(db_session, addr)


def test_transition_log_records_each_move(db_session):
    """§10: transitions record to the append-only log with optional reason."""
    _anchors(db_session)
    addr = "turtlesedge/turtletracks/spanish-beta"
    milestones.create(db_session, anchor="turtlesedge/turtletracks", name="spanish-beta")
    db_session.flush()
    milestones.activate(db_session, addr, reason="kickoff")
    milestones.achieve(db_session, addr, reason="shipped")
    log = milestones.transitions(db_session, addr)
    assert [(t["from_status"], t["to_status"]) for t in log] == [
        ("planned", "active"),
        ("active", "achieved"),
    ]
    assert log[0]["reason"] == "kickoff"
    assert log[1]["reason"] == "shipped"


# --- update -----------------------------------------------------------------


def test_get_malformed_address_is_not_found(db_session):
    """A grammar-invalid address raises MilestoneNotFoundError (not a validation
    error) so callers — the lifecycle verbs and the HTTP routes — fail clean."""
    _anchors(db_session)
    with pytest.raises(milestones.MilestoneNotFoundError):
        milestones.get(db_session, "turtlesedge/turtletracks/bad$name")
    with pytest.raises(milestones.MilestoneNotFoundError):
        milestones.get(db_session, "not a slug/x")


def test_update_outcome_and_target_date_only(db_session):
    _anchors(db_session)
    addr = "turtlesedge/turtletracks/spanish-beta"
    m = milestones.create(
        db_session, anchor="turtlesedge/turtletracks", name="spanish-beta"
    )
    db_session.flush()
    milestones.update(db_session, addr, outcome="new outcome")
    assert milestones.get(db_session, addr).outcome == "new outcome"
    # Omitting an arg leaves it unchanged; explicit None clears.
    milestones.update(db_session, addr, outcome=None)
    assert milestones.get(db_session, addr).outcome is None


# --- list -------------------------------------------------------------------


def test_list_filters_by_anchor_subtree_and_status(db_session):
    """§10: list filters — anchor SUBTREE (org surfaces repo-anchored rows too)
    and status."""
    _anchors(db_session)
    scopes.create(db_session, slug="other", name="Other", kind="org")
    milestones.create(db_session, anchor="turtlesedge", name="org-goal")
    milestones.create(db_session, anchor="turtlesedge/turtletracks", name="beta")
    milestones.create(db_session, anchor="turtlesedge/turtletracks", name="ga")
    milestones.create(db_session, anchor="other", name="elsewhere")
    db_session.flush()
    milestones.activate(db_session, "turtlesedge/turtletracks/beta")
    db_session.flush()

    # Org anchor subtree includes org- AND repo-anchored milestones.
    org_rows = milestones.list_milestones(db_session, anchor="turtlesedge")
    assert {r["address"] for r in org_rows} == {
        "turtlesedge/org-goal",
        "turtlesedge/turtletracks/beta",
        "turtlesedge/turtletracks/ga",
    }
    # Repo anchor narrows to just the repo-anchored ones.
    repo_rows = milestones.list_milestones(
        db_session, anchor="turtlesedge/turtletracks"
    )
    assert {r["address"] for r in repo_rows} == {
        "turtlesedge/turtletracks/beta",
        "turtlesedge/turtletracks/ga",
    }
    # Status filter.
    active = milestones.list_milestones(db_session, status="active")
    assert {r["address"] for r in active} == {"turtlesedge/turtletracks/beta"}
