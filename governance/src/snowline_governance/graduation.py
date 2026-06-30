"""Graduation — the one explicit crossing from shadow into the real graph.

Carried (functionality-first, NOT imported) from the frozen monolith's
`snowline_server.graduation` (decisions d3317b88 "graduation semantics",
99b92e1d "the principal split"). Translates a speculative `ShadowNode` into a
real `Decision` — the field copy `node.statement → decision`,
`node.rationale → rationale` — cherry-picking the shadow-dependency closure, and
stamps the bidirectional provenance markers:

  - `Decision.shadow_origin_node_id` / `shadow_origin_label` — the ONE-WAY,
    display-only backlink (a bare non-FK marker; real read surfaces show it but
    never traverse into shadow — "provenance, not citation", spec §6.4).
  - `ShadowNode.graduated_decision_id` — the forward marker (shadow→real, the
    permitted direction; a plain value here, NOT an FK — the inward-only
    invariant is structural in this DB) — drives idempotency + closure detection.

This performs REAL writes, so it lives on the `main` (real-write) MCP surface
the speculation/shadow agent does NOT hold — never on `/shadow/mcp` (decision
99b92e1d, "the principal split": the shadow agent drafts/proposes; the
ratifying principal on `main` executes). Surface placement enforces it.

THE ONE STRUCTURAL CHANGE from the monolith: scopes live in the PLATFORM now.
The monolith's `graduate(node, dest_scope)` took a scope SLUG and looked it up
in the local scope table; here the real decision is recorded via the soft scope
reference (`scope_slug` + `scope_id`) the CALLER resolved against the platform.
The natural destination is the branch's CRADLE scope (every branch is
scope-anchored, carrying its own `scope_slug` + `scope_id`); the caller may
override with a broader/org scope by passing `dest_scope_*`. Decision-graduation
only — the spec/artifact-emit path and the decision EMIT/webhook bus are
deferred.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from snowline_governance import decisions, shadow
from snowline_governance.models import Decision, ShadowBranch, ShadowNode


def _branch_address(session: Session, node: ShadowNode) -> tuple[str, str, uuid.UUID]:
    """The node's cradle scope `(slug, "<slug>:<branch>" address, scope_id)`, for
    the provenance label + the default graduation scope (cached so display never
    needs a shadow lookup)."""
    branch = session.get(ShadowBranch, node.branch_id)
    if branch is None:  # a node always has a branch (FK + cascade) — defensive
        return "?", "?", uuid.UUID(int=0)
    return branch.scope_slug, f"{branch.scope_slug}:{branch.name}", branch.scope_id


def _record_and_stamp(
    session: Session,
    dest_scope_slug: str,
    dest_scope_id: uuid.UUID | str,
    node: ShadowNode,
    decision: str,
    rationale: str | None,
) -> str:
    """`record_decision` the (possibly human-edited) text and stamp BOTH provenance
    markers — the real decision's one-way backlink and the node's forward marker.
    Returns the new real decision id."""
    _slug, address, _sid = _branch_address(session, node)
    rec = decisions.record_decision(
        session, dest_scope_slug, dest_scope_id, decision, rationale
    )
    dec = session.get(Decision, uuid.UUID(rec["id"]))
    dec.shadow_origin_node_id = str(node.id)
    dec.shadow_origin_label = address
    node.graduated_decision_id = dec.id
    session.flush()
    return rec["id"]


def _closure_postorder(session: Session, node: ShadowNode) -> list[ShadowNode]:
    """The shadow ancestors `node` cites transitively (within-branch, via
    `cited_node_id` edges), in DEPENDENCY ORDER — each ancestor before the node
    that cites it (post-order DFS). Cycle-safe via a seen-set. Excludes `node`.

    This is the cherry-pick closure (spec §6.4): graduating a node drags its
    un-graduated shadow-cited ancestors along, so the graduated decision's
    referenced reasoning comes into the real graph too. A real-decision citation
    (`cited_decision_id`) is already real — nothing to drag."""
    order: list[ShadowNode] = []
    # Seed with the start node so a self/cyclic citation can't pull `node` into
    # its OWN closure (which would double-graduate it under promote_closure).
    seen: set[uuid.UUID] = {node.id}

    def visit(n: ShadowNode) -> None:
        for c in shadow.list_citations(session, str(n.id)):
            cid = c.get("cited_node_id")
            if not cid:
                continue  # a real-decision citation is already real — nothing to drag
            key = uuid.UUID(cid)
            if key in seen:
                continue
            seen.add(key)
            anc = session.get(ShadowNode, key)
            if anc is None:
                continue
            visit(anc)          # the ancestor's own ancestors first …
            order.append(anc)   # … then the ancestor (post-order = dependency order)

    visit(node)
    return order


def graduate_node(
    session: Session,
    node_id: str,
    dest_scope_slug: str | None = None,
    dest_scope_id: uuid.UUID | str | None = None,
    decision: str | None = None,
    rationale: str | None = None,
    promote_closure: bool = True,
) -> dict:
    """Graduate a shadow node into a real `Decision`.

    Translates `node.statement → decision` / `node.rationale → rationale` (the
    caller may override either with a human-ratified edit via `decision`/
    `rationale`), records it at the destination scope, and stamps the
    bidirectional provenance (the real decision's `shadow_origin_node_id` +
    `shadow_origin_label`, the node's `graduated_decision_id`).

    SCOPE BINDING (the platform-owns-scopes carve): the destination defaults to
    the node's CRADLE scope — every branch is scope-anchored, so the branch's own
    `scope_slug` + `scope_id` are used when `dest_scope_*` are omitted. To
    graduate at a broader/org scope the caller resolves that scope against the
    platform and passes both `dest_scope_slug` + `dest_scope_id` (the soft
    reference, mirroring `record_decision`).

    IDEMPOTENT: a node already graduated returns its existing decision id (and the
    already-graduated closure ancestors), creating nothing new.

    CHERRY-PICK CLOSURE (`promote_closure`, default on, spec §6.4): the node's
    un-graduated shadow-cited ancestors are graduated FIRST, in dependency order,
    each with its OWN node text (only the top node carries the human's edits), so
    the graduated decision's referenced reasoning comes along into the real graph.
    """
    node = shadow.get_node(session, node_id)  # raises NodeNotFoundError
    if node.graduated_decision_id is not None:
        # Idempotent re-graduation: return the existing decision, create nothing.
        # Report any closure ancestors that were already graduated (so a caller
        # re-running with promote_closure sees the prior closure, not [] silently).
        promoted = (
            [
                str(anc.graduated_decision_id)
                for anc in _closure_postorder(session, node)
                if anc.graduated_decision_id is not None
            ]
            if promote_closure
            else []
        )
        return {
            "node_id": str(node.id),
            "decision_id": str(node.graduated_decision_id),
            "already_graduated": True,
            "closure_promoted": promoted,
        }

    # Default the destination to the node's cradle scope (the soft scope ref the
    # branch carries). An explicit dest scope (broader/org) overrides BOTH halves.
    cradle_slug, address, cradle_sid = _branch_address(session, node)
    if dest_scope_slug is None or dest_scope_id is None:
        dest_scope_slug, dest_scope_id = cradle_slug, cradle_sid

    promoted: list[str] = []
    if promote_closure:
        for anc in _closure_postorder(session, node):
            if anc.graduated_decision_id is None:
                promoted.append(
                    _record_and_stamp(
                        session,
                        dest_scope_slug,
                        dest_scope_id,
                        anc,
                        anc.statement,
                        anc.rationale,
                    )
                )

    dec_id = _record_and_stamp(
        session,
        dest_scope_slug,
        dest_scope_id,
        node,
        decision if decision is not None else node.statement,
        rationale if rationale is not None else node.rationale,
    )
    return {
        "node_id": str(node.id),
        "decision_id": dec_id,
        "scope": dest_scope_slug,
        "address": address,
        "already_graduated": False,
        "closure_promoted": promoted,
    }


# === Branch-level, agent-curated graduation + rejection ======================
# Graduate (or reject) a speculation AS A WHOLE: a drafting agent synthesizes the
# conversation into one END DECISION and curates which nodes support it; the human
# ratifies; the confirm records the end decision + the kept nodes. Both crossings
# are REAL writes — they mint real `Decision` rows — so they live HERE on the
# crossing layer and register on the `main` (real-write) MCP surface, never on
# `/shadow/mcp` (decision 99b92e1d "the principal split"). Carried, behavior-not-
# imports, from the frozen monolith's branch-graduation machinery (decisions
# 0c26be5c branch graduation, be803a2b the `shadow_origin_kind` discriminator).


def _record_and_stamp_branch(
    session: Session,
    dest_scope_slug: str,
    dest_scope_id: uuid.UUID | str,
    address: str,
    statement: str,
    rationale: str | None,
    kind: str,
) -> str:
    """Record a BRANCH-LEVEL real decision (a graduation END decision or a §7
    rejection) and stamp it with the branch address as origin — NO single
    `node_id` — plus the discriminating `kind`. The `kind` is what keeps the two
    otherwise identical branch-level markers from being read back as each other.
    Returns the new real decision id."""
    rec = decisions.record_decision(
        session, dest_scope_slug, dest_scope_id, statement, rationale
    )
    dec = session.get(Decision, uuid.UUID(rec["id"]))
    dec.shadow_origin_label = address  # node_id stays NULL (branch-level)
    dec.shadow_origin_kind = kind
    session.flush()
    return rec["id"]


def _existing_branch_decision(session: Session, address: str, kind: str):
    """The existing branch-level decision of `kind` for this address, if any — the
    natural-key idempotency lookup shared by graduation and rejection. A branch-
    level marker is `label == address AND node_id IS NULL`; `kind` disambiguates
    graduation from rejection. Legacy graduation decisions predating the `kind`
    column carry NULL, so a "graduation" lookup also accepts NULL (anything not
    explicitly a rejection) — only the rejection lookup is strict."""
    cond = (
        Decision.shadow_origin_kind == "rejection"
        if kind == "rejection"
        else Decision.shadow_origin_kind.is_distinct_from("rejection")
    )
    return session.scalar(
        select(Decision).where(
            Decision.shadow_origin_label == address,
            Decision.shadow_origin_node_id.is_(None),
            cond,
        )
    )


def graduate_branch(
    session: Session,
    scope_slug: str,
    name: str,
    dest_scope_slug: str,
    dest_scope_id: uuid.UUID | str,
    end_statement: str,
    end_rationale: str | None,
    include_node_ids: list[str],
) -> dict:
    """The human-ratified BRANCH graduation (decision 0c26be5c): record the
    synthesized END DECISION + each kept supporting node as real decisions, all
    provenance-linked to the branch. Un-selected nodes stay in shadow (the caller
    archives the branch separately if it wants to — graduation never auto-archives,
    since un-graduated nodes remain live speculation). The end decision carries the
    branch address as its origin with NO single node.

    SCOPE BINDING (the platform-owns-scopes carve): the destination scope is a SOFT
    reference the CALLER resolved against the platform (`dest_scope_slug` +
    `dest_scope_id`), mirroring `graduate_node` / `record_decision`; the caller
    defaults it to the branch's own cradle scope. The branch is addressed by its
    `scope_slug` + `name` (the `_get_branch_row` soft lookup).

    IDEMPOTENT: a branch-synthesized end decision is the ONE graduation decision
    carrying this branch's address as its origin label with NO `node_id`. If one
    already exists, this branch was already graduated — its end decision is returned
    rather than a SECOND competing one (a double-submit / retry guard). The `kind`
    filter keeps this from matching a rejection decision on the same branch
    (be803a2b): the two branch-level markers are otherwise identical in shape.
    """
    branch = shadow._get_branch_row(session, scope_slug, name)  # raises if unknown
    address = f"{branch.scope_slug}:{branch.name}"

    existing = _existing_branch_decision(session, address, "graduation")
    if existing is not None:
        return {
            "address": address,
            "scope": branch.scope_slug,
            "end_decision_id": str(existing.id),
            "promoted_node_ids": [],
            "already_graduated": True,
        }

    # The synthesized end decision — origin is the whole branch, not one node.
    end_decision_id = _record_and_stamp_branch(
        session, dest_scope_slug, dest_scope_id, address,
        end_statement, end_rationale, "graduation",
    )

    promoted: list[str] = []
    for nid in include_node_ids:
        try:
            key = uuid.UUID(str(nid))
        except (ValueError, TypeError):
            continue  # a malformed id is skipped, not a 500
        node = session.get(ShadowNode, key)
        # Only promote real, in-this-branch, not-yet-graduated nodes.
        if node is None or node.branch_id != branch.id or node.graduated_decision_id:
            continue
        promoted.append(
            _record_and_stamp(
                session, dest_scope_slug, dest_scope_id,
                node, node.statement, node.rationale,
            )
        )
    return {
        "address": address,
        "scope": dest_scope_slug,
        "end_decision_id": end_decision_id,
        "promoted_node_ids": promoted,
        "already_graduated": False,
    }


def record_branch_rejection(
    session: Session,
    scope_slug: str,
    scope_id: uuid.UUID | str,
    name: str,
    statement: str,
    rationale: str | None,
) -> dict:
    """Record the REJECTION of a whole speculation as a REAL decision in the
    branch's CRADLE scope (spec §7) — "we considered this line and chose against
    it", so the reasoning survives the line being shelved. The inverse facet of
    branch graduation; like graduation it crosses into the real graph (a real
    `record_decision`), so it lives HERE, not in the pure-shadow
    `shadow.archive_branch` (the flow "reject + archive a line" is
    `record_branch_rejection` here THEN `shadow.archive_branch`).

    Stamps the branch address as the decision's `shadow_origin_label` with NO
    `node_id` and a `shadow_origin_kind` of "rejection" — the kind is what
    distinguishes it from a graduation end decision on the same branch (be803a2b).

    SCOPE BINDING: the cradle scope is the BRANCH'S OWN scope — the caller resolves
    it against the platform and passes the soft reference (`scope_slug` +
    `scope_id`), exactly as it resolves the address lookup. (The rejection always
    lands in the cradle scope where the line was speculated; there is no broader/org
    override, unlike graduation's `dest_scope`.)

    IDEMPOTENT: a rejection decision already on this address (kind-filtered, so a
    graduation end decision on the same branch is NOT mistaken for one) is returned
    rather than recording a SECOND competing rejection.
    """
    branch = shadow._get_branch_row(session, scope_slug, name)  # raises if unknown
    address = f"{branch.scope_slug}:{branch.name}"

    existing = _existing_branch_decision(session, address, "rejection")
    if existing is not None:
        return {
            "address": address,
            "scope": branch.scope_slug,
            "rejection_decision_id": str(existing.id),
            "already_recorded": True,
        }

    # The rejection decision — origin is the whole branch, not one node. Recorded
    # in the cradle scope (the line was speculated there); node_id stays NULL.
    rejection_decision_id = _record_and_stamp_branch(
        session, scope_slug, scope_id, address, statement, rationale, "rejection",
    )
    return {
        "address": address,
        "scope": scope_slug,
        "rejection_decision_id": rejection_decision_id,
        "already_recorded": False,
    }
