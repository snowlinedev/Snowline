"""The decision substrate — record / supersede / read of `Decision` rows.

Carried (functionality-first, NOT imported) from the frozen monolith's
`snowline_server.decisions`: `record_decision`, `supersede_decision`,
`get_decision`, `list_decisions`, and the ancestor-inherited applicability
(`applicable_decisions`, the monolith's `related_decisions_for_scope`). The
leaf/supersession filter is carried in `branching.py`.

THE ONE STRUCTURAL CHANGE from the monolith: scopes live in the PLATFORM now, not
in this DB. So:
  - `record_decision(scope_slug, scope_id, ...)` takes the scope as a soft
    reference the CALLER resolved against the platform (governance does not look
    scopes up in a local table — there is none).
  - `applicable_decisions` gets the ancestor chain from an injected
    `ScopeClient` (the platform's `GET /scopes/{slug}/ancestors`) instead of the
    monolith's in-process `graph.ancestor_scopes_until_isolated`. The platform
    already halts at the first `isolated` node and the root; governance collects
    each chain scope's current decision leaves and tags inherited rows with
    `from_scope`. This is the §6.1 behavior, carried over HTTP.

Supersession stays INTRA-scope (a leaf is scoped by `scope_id`); the leaf filter
is per-scope. Decisions FK only to other decisions (the self-FK), never to a
scope (which is platform-owned, in another DB).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from snowline_governance import branching, replication
from snowline_governance.contract import (
    EVENT_DECISION_RECORDED,
    EVENT_DECISION_SUPERSEDED,
)
from snowline_governance.models import Decision
from snowline_governance.scope_client import ScopeClient


class DecisionNotFoundError(Exception):
    """No decision with the given id."""


class DecisionScopeMismatchError(Exception):
    """A `supersede_decision` call supplied a `scope` that doesn't match the prior
    decision's scope. Supersession stays within a scope — to "rewrite" a decision
    into a different scope, record a fresh one there."""


def _decision_summary(text: str, limit: int = 200) -> str:
    """The first line of a decision statement, clamped — the 'header' view."""
    first = (text or "").strip().split("\n", 1)[0].strip()
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _decision_row(d: Decision, *, from_scope: str | None = None) -> dict:
    """The read-shape for one `Decision` row. `from_scope` (a slug) records which
    ANCESTOR scope an inherited decision was resolved from — absent (None) on the
    reader's own-scope rows, so a reader can tell inherited-from-ancestor from
    own-scope (decision ee999c8d)."""
    row = {
        "id": str(d.id),
        "decision": d.decision,
        "rationale": d.rationale,
        "at": d.recorded_at.isoformat() if d.recorded_at else None,
        "supersedes": str(d.supersedes_id) if d.supersedes_id else None,
        "shadow_origin": (
            {"node_id": d.shadow_origin_node_id, "label": d.shadow_origin_label}
            if d.shadow_origin_label
            else None
        ),
    }
    if from_scope is not None:
        row["from_scope"] = from_scope
    return row


def _leaves_for_one_scope(
    session: Session,
    scope_id: uuid.UUID | str,
    *,
    include_superseded: bool,
    limit: int,
) -> list[Decision]:
    """The newest `limit` decisions in exactly ONE scope (matched by the STABLE
    `scope_id` soft reference — survives a platform-side slug rename), leaves-only
    unless `include_superseded`. Supersession is intra-scope, so the leaf filter
    is applied PER scope — the unit the inherited walk collects once per ancestor.
    """
    sid = scope_id if isinstance(scope_id, uuid.UUID) else uuid.UUID(str(scope_id))
    stmt = (
        select(Decision)
        .where(Decision.scope_id == sid)
        .order_by(Decision.recorded_at.desc())
        .limit(limit)
    )
    if not include_superseded:
        stmt = stmt.where(
            branching.leaf_filter(
                Decision.id,
                Decision.supersedes_id,
                Decision.scope_id == sid,
            )
        )
    return list(session.scalars(stmt))


def _leaves_for_scopes(
    session: Session,
    scope_ids: list[uuid.UUID],
    *,
    include_superseded: bool,
    limit: int,
) -> dict[uuid.UUID, list[Decision]]:
    """The newest `limit` decisions PER scope for a whole set of scopes, in ONE
    query — the batched form of `_leaves_for_one_scope` for the ancestor-inherited
    walk (issue #14: avoid a per-ancestor query storm).

    Supersession is strictly intra-scope (a superseding leaf carries the SAME
    `scope_id` as the row it supersedes), so the leaf filter's "is some sibling's
    supersedes target" sub-select can be scoped to the WHOLE set
    (`scope_id IN (:ids)`) and still resolve exactly as the per-scope filter does:
    a row only appears in the sub-select via a same-scope superseder. Rows are
    fetched newest-first across all scopes, then grouped in Python and each
    scope's group clamped to `limit` — identical to N separate per-scope queries
    each `ORDER BY recorded_at DESC LIMIT limit`.
    """
    by_scope: dict[uuid.UUID, list[Decision]] = {sid: [] for sid in scope_ids}
    if not scope_ids:
        return by_scope
    stmt = (
        select(Decision)
        .where(Decision.scope_id.in_(scope_ids))
        .order_by(Decision.recorded_at.desc(), Decision.id.desc())
    )
    if not include_superseded:
        stmt = stmt.where(
            branching.leaf_filter(
                Decision.id,
                Decision.supersedes_id,
                Decision.scope_id.in_(scope_ids),
            )
        )
    for d in session.scalars(stmt):
        bucket = by_scope.get(d.scope_id)
        if bucket is None:  # defensive; every chain id is pre-seeded above
            bucket = by_scope[d.scope_id] = []
        if len(bucket) < limit:  # per-scope LIMIT, applied in Python
            bucket.append(d)
    return by_scope


# --- writes -----------------------------------------------------------------


def record_decision(
    session: Session,
    scope_slug: str,
    scope_id: uuid.UUID | str,
    decision: str,
    rationale: str | None = None,
) -> dict:
    """Record a design/planning decision against a scope.

    The caller resolves the scope against the PLATFORM first (governance has no
    local scope table) and passes the soft reference: `scope_slug` + `scope_id`.
    Always creates a fresh leaf.
    """
    sid = scope_id if isinstance(scope_id, uuid.UUID) else uuid.UUID(str(scope_id))
    row = Decision(
        scope_id=sid,
        scope_slug=scope_slug,
        decision=decision,
        rationale=rationale,
    )
    session.add(row)
    session.flush()
    # EMIT (spec §7): write a pending WebhookDelivery per matching subscriber IN
    # this same transaction (transactional outbox) — a near-zero-cost no-op when
    # nobody is subscribed. The delivery loop signs + POSTs it asynchronously.
    replication.emit_decision_event(
        session, EVENT_DECISION_RECORDED, row, scope_slug
    )
    return {
        "id": str(row.id),
        "scope": scope_slug,
        "decision": _decision_summary(row.decision),
        "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
        "supersedes": None,
    }


def supersede_decision(
    session: Session,
    prior_decision_id: str,
    decision: str,
    rationale: str | None = None,
    scope: str | None = None,
) -> dict:
    """Record a new decision that supersedes an existing one. The prior row stays
    in the table for audit; future reads return only the new leaf by default.

    `scope` is optional and defaults to the prior decision's scope. Passing a
    `scope` slug different from the prior's raises — supersession is an
    intra-scope edit, not a cross-scope rewrite. Chains extend by one link.
    """
    try:
        prior_uuid = uuid.UUID(str(prior_decision_id))
    except (ValueError, AttributeError) as exc:
        raise DecisionNotFoundError(
            f"not a valid decision id: {prior_decision_id!r}"
        ) from exc
    prior = session.get(Decision, prior_uuid)
    if prior is None:
        raise DecisionNotFoundError(f"no decision with id {prior_decision_id!r}")
    if scope is not None and scope != prior.scope_slug:
        raise DecisionScopeMismatchError(
            f"supersede_decision: scope={scope!r} does not match prior decision's "
            f"scope {prior.scope_slug!r}. Record a fresh decision in the new "
            f"scope instead."
        )
    row = Decision(
        scope_id=prior.scope_id,
        scope_slug=prior.scope_slug,
        decision=decision,
        rationale=rationale,
        supersedes_id=prior.id,
    )
    session.add(row)
    session.flush()
    # EMIT (spec §7): the superseding leaf is itself a decision event — emit
    # `decision.superseded` for it (transactional outbox, same txn). Its payload's
    # `supersedes_id` carries the prior link a receiver orders by.
    replication.emit_decision_event(
        session, EVENT_DECISION_SUPERSEDED, row, prior.scope_slug
    )
    return {
        "id": str(row.id),
        "scope": prior.scope_slug,
        "decision": _decision_summary(row.decision),
        "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
        "supersedes": str(prior.id),
    }


# --- reads ------------------------------------------------------------------


def get_decision(session: Session, decision_id: str) -> dict:
    """The full decision — statement + rationale + lineage — by id. Read-only;
    raises on an unknown/invalid id."""
    try:
        key = uuid.UUID(str(decision_id))
    except (ValueError, TypeError):
        raise DecisionNotFoundError(
            f"not a valid decision id: {decision_id!r}"
        ) from None
    d = session.get(Decision, key)
    if d is None:
        raise DecisionNotFoundError(f"no decision with id {decision_id!r}")
    superseded_by = session.scalar(
        select(Decision.id).where(Decision.supersedes_id == d.id)
    )
    return {
        "id": str(d.id),
        "scope": d.scope_slug,
        "decision": d.decision,
        "rationale": d.rationale,
        "at": d.recorded_at.isoformat() if d.recorded_at else None,
        "supersedes": str(d.supersedes_id) if d.supersedes_id else None,
        "superseded_by": str(superseded_by) if superseded_by else None,
        "shadow_origin": (
            {"node_id": d.shadow_origin_node_id, "label": d.shadow_origin_label}
            if d.shadow_origin_label
            else None
        ),
    }


def _resolve_list_limit(limit: int | None, default: int = 50, cap: int = 500) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), cap))


def list_decisions(
    session: Session,
    scope_id: uuid.UUID | str,
    scope_slug: str,
    limit: int | None = None,
    include_superseded: bool = False,
) -> dict:
    """Browse a scope's decision history — headers only (id, one-line summary,
    recorded `at`, supersedes/superseded_by lineage markers), newest-first,
    payload-capped. EXACT-scope, matched by the STABLE `scope_id` (the caller
    resolves the slug against the platform first); `scope_slug` is carried for the
    response's display `scope` field only.

    By default (`include_superseded=False`) returns only chain leaves — the
    current decisions; `include_superseded=True` exposes full chains for audit.
    Capped at `limit` (default 50, max 500) with `items_total` carrying the true
    depth. Expand any row's full body via `get_decision(id)`. Read-only.

    Keying on `scope_id` (not the mutable slug) keeps pre-rename decisions visible
    after a platform-side slug rename — the slug changes, the id survives (#11).

    NOTE: the monolith's `subtree=True` descendant span needs the platform's scope
    tree and lands when artifact governs-matching does (a later increment).
    """
    sid = scope_id if isinstance(scope_id, uuid.UUID) else uuid.UUID(str(scope_id))
    lim = _resolve_list_limit(limit)
    stmt = (
        select(Decision)
        .where(Decision.scope_id == sid)
        .order_by(Decision.recorded_at.desc(), Decision.id.desc())
    )
    if not include_superseded:
        stmt = stmt.where(
            branching.leaf_filter(
                Decision.id,
                Decision.supersedes_id,
                Decision.scope_id == sid,
            )
        )
    rows = list(session.scalars(stmt))
    superseder: dict[uuid.UUID, uuid.UUID] = {}
    if include_superseded:
        for prior_id, succ_id in session.execute(
            select(Decision.supersedes_id, Decision.id).where(
                Decision.scope_id == sid,
                Decision.supersedes_id.is_not(None),
            )
        ):
            superseder[prior_id] = succ_id
    return {
        "scope": scope_slug,
        "include_superseded": include_superseded,
        "decisions": [_decision_list_row(d, superseder) for d in rows[:lim]],
        "items_total": len(rows),
    }


def _decision_list_row(
    d: Decision, superseder: dict[uuid.UUID, uuid.UUID]
) -> dict:
    """Header row for `list_decisions` — id + one-line summary + at + lineage
    markers. `get_decision(id)` expands the full body + rationale on demand."""
    succ = superseder.get(d.id)
    return {
        "id": str(d.id),
        "decision": _decision_summary(d.decision),
        "at": d.recorded_at.isoformat() if d.recorded_at else None,
        "supersedes": str(d.supersedes_id) if d.supersedes_id else None,
        "superseded_by": str(succ) if succ else None,
    }


def applicable_decisions(
    session: Session,
    scope_slug: str,
    scope_client: ScopeClient,
    *,
    include_superseded: bool = False,
    limit: int = 50,
) -> dict:
    """Decisions APPLICABLE at a scope, ANCESTOR-INHERITED (decision ee999c8d) —
    the §6.1 behavior, carried over HTTP.

    The ancestor chain lives in the PLATFORM now, so this asks the injected
    `scope_client` for it (`GET /scopes/{slug}/ancestors`): the reader's own
    scope first, then each `parent_id` ancestor nearest-first, the platform
    already HALTING at the first `isolated` node and the forest root. For each
    chain scope, governance collects that scope's current decision leaves (the
    leaf filter stays PER scope — supersession is intra-scope). Each INHERITED
    row carries `from_scope` (the ancestor slug it came from); the reader's OWN
    rows omit it.

    The result is OWN scope first, then ancestors by distance, newest-first
    WITHIN each scope, capped at `limit` — so the cap never truncates the
    reader's own current decisions in favour of a fresher ancestor's (#651
    review). `scope_client` is injectable so tests stub it without a running
    platform; the chain ROWS carry `id` + `slug` (the platform's `to_row` shape).

    Leaves are collected by each chain scope's STABLE `id` (not its slug), so a
    platform-side slug rename can't make pre-rename governance invisible — the
    slug the platform now serves drives `from_scope` (display) only (#11).
    """
    chain = scope_client.ancestors(scope_slug)
    chain_ids = [uuid.UUID(str(sc["id"])) for sc in chain]
    # ONE query for every chain scope's leaves (issue #14), then group + tag in
    # Python — preserving the walk's own-first / nearest-ancestor-next order and
    # the per-scope LIMIT (supersession is intra-scope, so the batched leaf filter
    # is exactly the per-scope one).
    by_scope = _leaves_for_scopes(
        session,
        chain_ids,
        include_superseded=include_superseded,
        limit=limit,
    )
    collected: list[dict] = []
    for depth, (sc, sid) in enumerate(zip(chain, chain_ids)):
        sc_slug = sc["slug"]
        for d in by_scope.get(sid, ()):
            collected.append(
                _decision_row(d, from_scope=None if depth == 0 else sc_slug)
            )
    total = len(collected)  # true depth BEFORE the cap, so a reader sees "more"
    collected = collected[:limit]
    return {"scope": scope_slug, "decisions": collected, "items_total": total}
