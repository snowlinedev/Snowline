"""§6.1 concurrent-sibling state — the `concurrent_with` markers and the
`unreconciled` read view (replication-continuity §6.1, #79).

A `DecisionConcurrence` row is a NORMALIZED unordered pair, written at ingest
by the detection walk (`replication_apply._detect_concurrent_siblings`) and
never deleted. "Unreconciled" is DERIVED, not stored: a pair is open while BOTH
members are still supersession leaves. Reconciliation is ordinary governance —
recording a supersession over either member (a normal event that replicates
normally) makes that member a non-leaf, and the flag clears on both sides the
moment the event applies, with no marker write and nothing to keep in sync.

The reads here feed the three §6.1 surfacing seams: `get_decision`'s
`concurrent_with` list, the `unreconciled_decisions` MCP tool, and the
`/ui-api` stat widget — first-class state, not a log line.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from snowline_governance.models import Decision, DecisionConcurrence


def _normalized(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if a < b else (b, a)


def flag_pair(session: Session, a: uuid.UUID, b: uuid.UUID) -> bool:
    """Record the pair (idempotent — detection reruns on redelivery/re-apply).
    Returns True when a NEW pair was flagged."""
    lo, hi = _normalized(a, b)
    if session.get(DecisionConcurrence, (lo, hi)) is not None:
        return False
    session.add(DecisionConcurrence(decision_id=lo, concurrent_with_id=hi))
    session.flush()
    return True


def _superseded_ids(session: Session, ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """The subset of `ids` that are no longer leaves (something supersedes
    them) — the derived "reconciled" signal."""
    if not ids:
        return set()
    return set(
        session.scalars(
            select(Decision.supersedes_id).where(Decision.supersedes_id.in_(ids))
        )
    )


def concurrent_with(session: Session, decision_id: uuid.UUID) -> list[str]:
    """The OTHER member of every still-unreconciled pair this decision is in
    (sorted for a stable read shape). Empty for an unflagged or reconciled
    decision."""
    rows = session.scalars(
        select(DecisionConcurrence).where(
            or_(
                DecisionConcurrence.decision_id == decision_id,
                DecisionConcurrence.concurrent_with_id == decision_id,
            )
        )
    ).all()
    if not rows:
        return []
    members = {r.decision_id for r in rows} | {r.concurrent_with_id for r in rows}
    superseded = _superseded_ids(session, members)
    others = {
        (r.concurrent_with_id if r.decision_id == decision_id else r.decision_id)
        for r in rows
        if r.decision_id not in superseded
        and r.concurrent_with_id not in superseded
    }
    return sorted(str(o) for o in others)


def _pair_member(d: Decision | None, member_id: uuid.UUID) -> dict:
    if d is None:  # marker for a decision not (yet) applied locally — defensive
        return {"id": str(member_id)}
    return {
        "id": str(d.id),
        "scope": d.scope_slug,
        "decision": (d.decision or "").strip().split("\n", 1)[0][:200],
        "at": d.recorded_at.isoformat() if d.recorded_at else None,
    }


def unreconciled_pairs(session: Session, limit: int = 50) -> dict:
    """The §6.1 `unreconciled` view: every flagged pair whose BOTH members are
    still leaves, oldest flag first, each member as a decision header. This is
    what the daily-driver agent (tool) and the dashboard widget read."""
    rows = session.scalars(
        select(DecisionConcurrence).order_by(
            DecisionConcurrence.created_at, DecisionConcurrence.decision_id
        )
    ).all()
    members = {r.decision_id for r in rows} | {r.concurrent_with_id for r in rows}
    superseded = _superseded_ids(session, members)
    open_rows = [
        r
        for r in rows
        if r.decision_id not in superseded
        and r.concurrent_with_id not in superseded
    ]
    decisions = {
        d.id: d
        for d in session.scalars(
            select(Decision).where(
                Decision.id.in_(
                    {r.decision_id for r in open_rows}
                    | {r.concurrent_with_id for r in open_rows}
                )
            )
        )
    } if open_rows else {}
    pairs = [
        {
            "decisions": [
                _pair_member(decisions.get(r.decision_id), r.decision_id),
                _pair_member(
                    decisions.get(r.concurrent_with_id), r.concurrent_with_id
                ),
            ],
            "flagged_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in open_rows
    ]
    return {"pairs": pairs[:limit], "items_total": len(pairs)}


def unreconciled_count(session: Session) -> int:
    """The widget's number — open pairs only."""
    return unreconciled_pairs(session)["items_total"]
