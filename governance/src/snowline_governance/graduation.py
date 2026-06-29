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
