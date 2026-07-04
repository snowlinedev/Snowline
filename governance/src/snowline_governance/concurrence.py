"""§6.1 concurrent-sibling state — the `concurrent_with` markers and the
`unreconciled` read view (replication-continuity §6.1, #79).

A `DecisionConcurrence` row is a NORMALIZED unordered pair, written at ingest
by the detection walk (`replication_apply._detect_concurrent_siblings`) and
never deleted. "Unreconciled" is DERIVED, not stored: a pair is open while BOTH
members are still supersession leaves AND the pair is not explicitly marked
compatible (`marked_compatible_at IS NULL`, #97). Reconciliation is ordinary
governance — recording a supersession over either member (a normal event that
replicates normally) makes that member a non-leaf, and the flag clears on both
sides the moment the event applies, with no marker write and nothing to keep in
sync. The SECOND clearing path (#97, the other half of §6.1's sentence) is an
explicit `decision.marked_compatible` judgment: a permanent, idempotent stamp on
the immutable pair (`mark_compatible` below) that both sides converge to.

The reads here feed the three §6.1 surfacing seams: `get_decision`'s
`concurrent_with` list, the `unreconciled_decisions` MCP tool, and the
`/ui-api` stat widget — first-class state, not a log line.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from snowline_governance.models import Decision, DecisionConcurrence


def _normalized(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if a < b else (b, a)


def flag_pair(session: Session, a: uuid.UUID, b: uuid.UUID) -> bool:
    """Record the pair (idempotent — detection reruns on redelivery/re-apply).
    Returns True when a NEW pair was flagged. Tolerates a pre-existing row: a
    `decision.marked_compatible` mark can UPSERT the pair before this side's own
    detection runs (#97), so a later flag simply finds it and no-ops — never
    clobbering `marked_compatible_at`."""
    lo, hi = _normalized(a, b)
    if session.get(DecisionConcurrence, (lo, hi)) is not None:
        return False
    session.add(DecisionConcurrence(decision_id=lo, concurrent_with_id=hi))
    session.flush()
    return True


def get_pair(
    session: Session, a: uuid.UUID, b: uuid.UUID
) -> DecisionConcurrence | None:
    """The concurrence row for the normalized pair, or None if the system never
    flagged it (the `mark_compatible` verb validates this — you can only judge a
    pair detection actually surfaced)."""
    return session.get(DecisionConcurrence, _normalized(a, b))


def mark_compatible(
    session: Session,
    a: uuid.UUID,
    b: uuid.UUID,
    at: datetime,
    *,
    create_if_absent: bool = False,
) -> DecisionConcurrence | None:
    """Anchor §6.1's explicit compatibility judgment to the normalized immutable
    pair (#97). PERMANENT + IDEMPOTENT: the EARLIEST `at` wins, so re-marking is a
    no-op and two independently-authored marks converge order-independently (no
    LWW register, no clock tiebreak, no unmark verb).

    `create_if_absent` distinguishes the two call sites:
      * The LOCAL verb passes False — the tool has already validated the row
        exists (`get_pair`), so an absent row is a programming error → None.
      * APPLY passes True — a mark can arrive before THIS side has detected the
        pair itself (the second member/its detection is still in flight), so the
        row materializes here; the later detection's `flag_pair` then no-ops.

    Returns the row, or None when absent and `create_if_absent` is False."""
    lo, hi = _normalized(a, b)
    row = session.get(DecisionConcurrence, (lo, hi))
    if row is None:
        if not create_if_absent:
            return None
        row = DecisionConcurrence(
            decision_id=lo, concurrent_with_id=hi, marked_compatible_at=at
        )
        session.add(row)
        session.flush()
        return row
    if row.marked_compatible_at is None or at < row.marked_compatible_at:
        row.marked_compatible_at = at
        session.flush()
    return row


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


def concurrent_with(session: Session, decision_id: uuid.UUID) -> list[dict]:
    """Every concurrent-sibling pair this decision is in that a supersession has
    NOT reconciled — each as `{"id", "compatible"}`, sorted by id for a stable
    read shape. A pair explicitly marked compatible (#97) STILL appears, with
    `compatible=True`: the marker is real history the reader should see, not a
    reason to hide the pair. Entries drop only when a member is superseded (the
    derived leaf rule). Empty for an unflagged or supersession-reconciled
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
    entries = [
        {
            "id": str(
                r.concurrent_with_id
                if r.decision_id == decision_id
                else r.decision_id
            ),
            "compatible": r.marked_compatible_at is not None,
        }
        for r in rows
        if r.decision_id not in superseded
        and r.concurrent_with_id not in superseded
    ]
    return sorted(entries, key=lambda e: e["id"])


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
    still leaves AND that has not been explicitly marked compatible (#97), oldest
    flag first, each member as a decision header. This is what the daily-driver
    agent (tool) and the dashboard widget read."""
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
        if r.marked_compatible_at is None
        and r.decision_id not in superseded
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
