"""Shared supersession-DAG helpers — the leaf filter, carried (not imported)
from the frozen monolith's `snowline_server.branching`.

A node is a **leaf** when nothing supersedes it; leaves are the current
candidates and surface with ZERO inference. A **branch/fork point** is a node
that ≥2 successors point at. These are column-driven so they compose into a
wider `select(Decision).where(scope_filter, leaf_filter(...))` — one definition
of "current", scoped per scope (supersession is intra-scope).

This increment carries `leaf_filter` (the one the decision reads need) and the
`branch_points` companion. The artifact-version carrier reuses the same shape in
a later increment, exactly as in the monolith.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session


def _superseded_subq(
    supersedes_col: InstrumentedAttribute, scope_filter: ColumnElement
):
    """Sub-select of the ids that some sibling (within the same scope) supersedes.
    Filters out NULLs so it's safe inside a `NOT IN`."""
    return select(supersedes_col).where(scope_filter, supersedes_col.is_not(None))


def leaf_filter(
    id_col: InstrumentedAttribute,
    supersedes_col: InstrumentedAttribute,
    scope_filter: ColumnElement,
) -> ColumnElement:
    """A WHERE condition selecting **leaf** rows within `scope_filter`: rows whose
    id is not referenced by any sibling's `supersedes_id`."""
    return id_col.not_in(_superseded_subq(supersedes_col, scope_filter))


def branch_points(
    session: Session,
    supersedes_col: InstrumentedAttribute,
    scope_filter: ColumnElement,
) -> list:
    """Node ids within the scope that ≥2 successors supersede — the fork points.
    Empty when the topology is a plain chain (the common case)."""
    rows = session.execute(
        select(supersedes_col)
        .where(scope_filter, supersedes_col.is_not(None))
        .group_by(supersedes_col)
        .having(func.count() >= 2)
    ).all()
    return [r[0] for r in rows]
