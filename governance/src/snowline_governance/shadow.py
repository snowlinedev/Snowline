"""The shadow / speculation substrate — data access over the shadow subgraph.

Carried (functionality-first, NOT imported) from the frozen monolith's
`snowline_server.shadow`: named speculative branches per scope, speculative
decision nodes, the inward-only citation edge, and a full-text corpus search over
the shadow content. The deliberately *not-yet-real* inverse of `record_decision`.

TWO STRUCTURAL CHANGES from the monolith, both flowing from the platform owning
scopes + the inward-only isolation invariant (spec §3 / §6.4):

  - **Soft scope refs.** Scopes live in the PLATFORM, so a `create_branch`/
    `list_branches`/`get_branch` call takes the scope as a soft reference the
    CALLER resolved against the platform (`scope_slug` + `scope_id`), exactly as
    `decisions.record_decision` does — there is no local scope table to look up,
    and a branch is anchored by the STABLE `scope_id` (#11). `corpus_search`
    narrows to the EXACT resolved `scope_id` (the monolith narrowed to the scope
    SUBTREE via the in-process scope tree, which lives in the platform now — an
    exact-scope narrowing keeps this module import-pure; subtree narrowing can
    ride the `ScopeClient` in a later increment if needed).

  - **Citation-to-real is validated, not FK'd.** The inward-only invariant is
    held STRUCTURALLY in the schema (no shadow→real FK anywhere — `add_citation`
    enforces it: a node may cite another node in its OWN branch, or a real
    decision, and the real decision's existence is checked here against the
    `decisions` table; the reverse never exists). A cross-branch citation and a
    real→shadow direction both raise.

This module is plain functions over a `Session`, mirroring `decisions.py`'s
style. It does NOT touch any MCP surface — the `shadow` FastMCP surface
(`mcp_surface.build_shadow_surface`) wraps these. GRADUATION (shadow node → real
decision) is a separate follow-up PR; this layer never writes the real graph.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from snowline_plugin_sdk.ui import UI_WRITE_BODY_LIMIT

from snowline_governance import replication_stream
from snowline_governance.contract import (
    EVENT_SHADOW_BRANCH_ARCHIVED,
    EVENT_SHADOW_BRANCH_CREATED,
    EVENT_SHADOW_CITATION_ADDED,
    EVENT_SHADOW_CONVERSATION_APPENDED,
    EVENT_SHADOW_NODE_ADDED,
    EVENT_SHADOW_NOTES_SET,
)
from snowline_governance.models import (
    DEFAULT_SHADOW_BRANCH_STATUS,
    SHADOW_BRANCH_STATUS_ARCHIVED,
    Decision,
    ShadowBranch,
    ShadowConversationEvent,
    ShadowNode,
    ShadowNodeCitation,
)

# The conversation-event kinds this surface writes/reads (spec §2): a `message`
# (human OR agent) and an `agent.error` (a failed phase-2 turn, kept VISIBLE in
# the thread — fail-visible, ui-shell §4.4). Both count as "conversation".
CONVERSATION_MESSAGE_KIND = "message"
CONVERSATION_ERROR_KIND = "agent.error"
CONVERSATION_KINDS: tuple[str, ...] = (
    CONVERSATION_MESSAGE_KIND,
    CONVERSATION_ERROR_KIND,
)
# The only message authors: the UI composer's browser seam writes `human`; MCP
# sessions and the turn-runner write `agent`. Enforced at `add_message` so the
# thread renderer's you/agent mapping can't be fed a third value.
CONVERSATION_AUTHORS: frozenset[str] = frozenset({"human", "agent"})
# The `get_branch` MCP tail cap (spec §5): the last ~50 conversation events so a
# re-entering session sees what was said without dragging the whole log.
CONVERSATION_TAIL_LIMIT = 50

# Corpus search limit budget — local constants (the monolith shares search.py's
# SEARCH_* budget; governance has no real `search` surface, so they live here).
CORPUS_SEARCH_DEFAULT_LIMIT = 20
CORPUS_SEARCH_MAX_LIMIT = 100
_TS_CONFIG = "english"

# kind → corpus order (ties in rank stay deterministic). Branches before their
# nodes; attached real decisions last.
_CORPUS_KIND_ORDER = ("shadow_branch", "shadow_node", "decision")


class DuplicateBranchError(Exception):
    """A branch with that name already exists in the scope. Branch names are
    unique within a scope (§4 addressing `<scope>:<name>`)."""


class BranchNotFoundError(Exception):
    """No shadow branch with the given `<scope>:<name>` address (or branch id)."""


class BranchArchivedError(Exception):
    """The branch is archived — a concluded speculation line accepts no new
    conversation (spec §5: the composer is already disabled via `disabled_when`,
    but the service owns the semantics). The `/ui-api` route turns this into 409."""


class MessageValidationError(ValueError):
    """A conversation message is blank or exceeds the proxy's body cap
    (`UI_WRITE_BODY_LIMIT`). A `ValueError` subclass so the route can map it to
    422 the same way it treats malformed input."""


class NodeNotFoundError(Exception):
    """No shadow node with the given id."""


class CitationTargetError(Exception):
    """A citation must have exactly one target — another shadow node in the SAME
    branch XOR a real decision (§6.4) — and the target must exist."""


# --- serializers -----------------------------------------------------------


def _branch_dict(branch: ShadowBranch, *, nodes=None) -> dict:
    out = {
        "id": str(branch.id),
        "scope": branch.scope_slug,
        "name": branch.name,
        # The addressable handle (§4): `<scope>:<branch>`, no global namespace.
        "address": f"{branch.scope_slug}:{branch.name}",
        "status": branch.status,
        "narrative_notes": branch.narrative_notes,
        "archived_at": (
            branch.archived_at.isoformat() if branch.archived_at else None
        ),
        "created_at": branch.created_at.isoformat() if branch.created_at else None,
        "updated_at": branch.updated_at.isoformat() if branch.updated_at else None,
    }
    if nodes is not None:
        out["nodes"] = [_node_dict(n) for n in nodes]
    return out


def _node_dict(node: ShadowNode) -> dict:
    return {
        "id": str(node.id),
        "branch_id": str(node.branch_id),
        "statement": node.statement,
        "rationale": node.rationale,
        # The real decision this node graduated into (§4), or None while still
        # speculative. Graduation is a later PR; this stays None for now.
        "graduated_decision_id": (
            str(node.graduated_decision_id) if node.graduated_decision_id else None
        ),
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }


def _citation_dict(c: ShadowNodeCitation) -> dict:
    return {
        "id": str(c.id),
        "node_id": str(c.node_id),
        # Exactly one of these is set (§6.4 XOR): a within-shadow dependency, or
        # the permitted inward reference to a real decision.
        "cited_node_id": str(c.cited_node_id) if c.cited_node_id else None,
        "cited_decision_id": (
            str(c.cited_decision_id) if c.cited_decision_id else None
        ),
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _conversation_event_dict(event: ShadowConversationEvent) -> dict:
    """The append receipt (spec §5): id, the per-branch monotonic `seq` (doubles
    as the resume cursor), `kind`, the untyped `payload`, and `created_at`."""
    return {
        "id": str(event.id),
        "seq": event.seq,
        "kind": event.kind,
        "payload": event.payload,
        "created_at": (
            event.created_at.isoformat() if event.created_at else None
        ),
    }


def _conversation_tail_dict(event: ShadowConversationEvent) -> dict:
    """A normalized conversation entry for the `get_branch` MCP tail (spec §5) —
    `{seq, author, markdown, at}`. A `message` carries its payload `author`
    (`human`/`agent` — kept raw, honest for an agent reading the log, NOT the
    UI's `you`/`agent` display mapping) and `markdown`; an `agent.error` renders
    as an `agent` entry whose `markdown` is the error text (fail-visible §2)."""
    payload = event.payload or {}
    if event.kind == CONVERSATION_ERROR_KIND:
        author = "agent"
        markdown = payload.get("error", "")
    else:
        author = payload.get("author")
        markdown = payload.get("markdown", "")
    return {
        "seq": event.seq,
        "author": author,
        "markdown": markdown,
        "at": event.created_at.isoformat() if event.created_at else None,
    }


# --- internal helpers ------------------------------------------------------


def _parse_uuid(value, err: type[Exception], label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise err(f"not a valid {label}: {value!r}") from exc


def _get_branch_row(
    session: Session, scope_slug: str, name: str
) -> ShadowBranch:
    """The branch row addressed by `<scope>:<name>` (matched by the soft
    `scope_slug` reference)."""
    branch = session.scalar(
        select(ShadowBranch).where(
            ShadowBranch.scope_slug == scope_slug, ShadowBranch.name == name
        )
    )
    if branch is None:
        raise BranchNotFoundError(f"no shadow branch {scope_slug}:{name!r}")
    return branch


def _branch_nodes(session: Session, branch_id: uuid.UUID) -> list[ShadowNode]:
    return list(
        session.scalars(
            select(ShadowNode)
            .where(ShadowNode.branch_id == branch_id)
            .order_by(ShadowNode.created_at, ShadowNode.id)
        )
    )


def _branch_conversation_events(
    session: Session,
    branch_id: uuid.UUID,
    *,
    kinds: tuple[str, ...] = CONVERSATION_KINDS,
    tail: int | None = None,
) -> list[ShadowConversationEvent]:
    """A branch's conversation events, oldest-first by the monotonic `seq`
    (chronological — `seq` is allocated in `created_at` order). Narrowed to
    `kinds` (default: message + agent.error, the two conversation kinds §2).
    `tail=N` returns the LAST N (still oldest-first) — the `get_branch` tail cap."""
    stmt = select(ShadowConversationEvent).where(
        ShadowConversationEvent.branch_id == branch_id,
        ShadowConversationEvent.kind.in_(kinds),
    )
    if tail is not None:
        # Last `tail` by seq, then reversed to oldest-first (a window read, not
        # the whole log).
        rows = list(
            session.scalars(
                stmt.order_by(ShadowConversationEvent.seq.desc()).limit(tail)
            )
        )
        rows.reverse()
        return rows
    return list(session.scalars(stmt.order_by(ShadowConversationEvent.seq)))


def _resolve_list_limit(
    limit: int | None, default: int, maximum: int
) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), maximum))


# --- branches --------------------------------------------------------------


def create_branch(
    session: Session,
    scope_slug: str,
    scope_id: uuid.UUID | str,
    name: str,
    narrative_notes: str | None = None,
) -> dict:
    """Open a new speculative branch in a scope. The caller resolves the scope
    against the PLATFORM first (governance has no local scope table) and passes
    the soft reference (`scope_slug` + `scope_id`). Branch names are unique within
    the scope — a clash raises `DuplicateBranchError` (the DB unique constraint is
    the backstop)."""
    sid = scope_id if isinstance(scope_id, uuid.UUID) else uuid.UUID(str(scope_id))
    existing = session.scalar(
        select(ShadowBranch).where(
            ShadowBranch.scope_id == sid, ShadowBranch.name == name
        )
    )
    if existing is not None:
        raise DuplicateBranchError(
            f"shadow branch {scope_slug}:{name!r} already exists"
        )
    branch = ShadowBranch(
        scope_id=sid,
        scope_slug=scope_slug,
        name=name,
        narrative_notes=narrative_notes,
    )
    session.add(branch)
    session.flush()
    # STREAM emit (replication-continuity §4 coverage, #79) — same transaction.
    replication_stream.emit(
        session,
        EVENT_SHADOW_BRANCH_CREATED,
        replication_stream.branch_created_payload(branch),
    )
    return _branch_dict(branch, nodes=[])


def list_branches(
    session: Session,
    scope_slug: str,
    scope_id: uuid.UUID | str,
    include_done: bool = False,
) -> list[dict]:
    """Branches in a scope, newest first (matched by the STABLE `scope_id`). Done
    (archived) branches are hidden by default (§6.4) — they represent concluded
    speculation, not active work. Pass `include_done=True` to reveal them.
    `get_branch` (single fetch) always fetches regardless of status."""
    sid = scope_id if isinstance(scope_id, uuid.UUID) else uuid.UUID(str(scope_id))
    stmt = select(ShadowBranch).where(ShadowBranch.scope_id == sid)
    if not include_done:
        stmt = stmt.where(ShadowBranch.status == DEFAULT_SHADOW_BRANCH_STATUS)
    stmt = stmt.order_by(ShadowBranch.created_at.desc(), ShadowBranch.id)
    return [_branch_dict(b) for b in session.scalars(stmt)]


def get_branch(session: Session, scope_slug: str, name: str) -> dict:
    """A branch with its nodes (the verdicts), narrative notes (the reasoning),
    and a `conversation` tail — the re-entry surface for a speculation line (§4).
    Raises `BranchNotFoundError` if the address is unknown.

    MCP parity (spec §5): the `conversation` tail is the LAST
    `CONVERSATION_TAIL_LIMIT` conversation events (messages + agent errors),
    oldest-first, each `{seq, author, markdown, at}` — so a Claude Code session
    re-entering the branch sees what was said in the UI (acceptance §7.6)."""
    branch = _get_branch_row(session, scope_slug, name)
    nodes = _branch_nodes(session, branch.id)
    out = _branch_dict(branch, nodes=nodes)
    out["conversation"] = [
        _conversation_tail_dict(ev)
        for ev in _branch_conversation_events(
            session, branch.id, tail=CONVERSATION_TAIL_LIMIT
        )
    ]
    return out


def set_narrative_notes(
    session: Session, scope_slug: str, name: str, narrative_notes: str | None
) -> dict:
    """Replace a branch's narrative-notes doc — the running thread that
    reconstructs the reasoning on re-entry (§4)."""
    branch = _get_branch_row(session, scope_slug, name)
    branch.narrative_notes = narrative_notes
    session.flush()
    # STREAM emit — an in-place LWW register write (§6): the emit hook also
    # records this write's (at, source) coordinate for conflict resolution.
    replication_stream.emit(
        session,
        EVENT_SHADOW_NOTES_SET,
        replication_stream.notes_set_payload(branch),
    )
    return _branch_dict(branch)


def archive_branch(session: Session, scope_slug: str, name: str) -> dict:
    """Archive a branch — the active→archived status flip (§6.4): a KEPT row
    (listable via `include_done`), not a delete. A PURE shadow operation (no real
    write), so it lives in this module and registers on the SHADOW surface — the
    inverse of `graduation.record_branch_rejection` (which mints a real decision and
    lives on `main`). The flow "reject + archive a line" pairs the two:
    `record_branch_rejection` records WHY (real), then `archive_branch` shelves the
    line (shadow). Graduation does NOT auto-archive — un-selected nodes stay live
    speculation.

    Idempotent: re-archiving an already-archived branch is a no-op that returns it,
    PINNING `archived_at` to the original archival (deliberately not refreshed)."""
    branch = _get_branch_row(session, scope_slug, name)
    if branch.status != SHADOW_BRANCH_STATUS_ARCHIVED:
        branch.status = SHADOW_BRANCH_STATUS_ARCHIVED
        # The timestamp columns in this schema are naive UTC (server `func.now()`),
        # so stamp a naive UTC value — a tz-aware one would round-trip differently
        # than the naive value a later read returns.
        branch.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.flush()
        # STREAM emit — inside the flip, so an idempotent re-archive emits
        # nothing (there was no write). Apply converges `archived_at` to the
        # EARLIEST archival (the "pinned to the original" semantics, §6).
        replication_stream.emit(
            session,
            EVENT_SHADOW_BRANCH_ARCHIVED,
            replication_stream.branch_archived_payload(branch),
        )
    return _branch_dict(branch)


# --- nodes -----------------------------------------------------------------


def add_node(
    session: Session,
    scope_slug: str,
    name: str,
    statement: str,
    rationale: str | None = None,
) -> dict:
    """Add a speculative-decision node to a branch. `statement` is the
    not-yet-real decision; `rationale` its crisp why. Individually addressable so
    graduation can later cherry-pick it (§4)."""
    branch = _get_branch_row(session, scope_slug, name)
    node = ShadowNode(
        branch_id=branch.id, statement=statement, rationale=rationale
    )
    session.add(node)
    session.flush()
    replication_stream.emit(
        session,
        EVENT_SHADOW_NODE_ADDED,
        replication_stream.node_added_payload(node),
    )
    return _node_dict(node)


def get_node(session: Session, node_id: str) -> ShadowNode:
    node = session.get(
        ShadowNode, _parse_uuid(node_id, NodeNotFoundError, "node id")
    )
    if node is None:
        raise NodeNotFoundError(f"no shadow node with id {node_id!r}")
    return node


# --- citations -------------------------------------------------------------


def add_citation(
    session: Session,
    node_id: str,
    *,
    cited_node_id: str | None = None,
    cited_decision_id: str | None = None,
) -> dict:
    """Record a citation FROM `node_id`. Exactly one target (§6.4): another shadow
    node in the SAME branch (`cited_node_id` — a within-shadow dependency) XOR a
    real decision (`cited_decision_id` — the permitted INWARD reference). The
    reverse direction does not exist in the schema: nothing real may ever cite a
    shadow node, and there is no shadow→real FK — the real decision's existence is
    validated HERE against the `decisions` table, not by a foreign key."""
    if (cited_node_id is None) == (cited_decision_id is None):
        raise CitationTargetError(
            "a citation needs exactly one target: cited_node_id XOR "
            "cited_decision_id"
        )
    node = get_node(session, node_id)

    cited_node_uuid = None
    cited_decision_uuid = None
    if cited_node_id is not None:
        # The target must exist AND live in the SAME branch: branches are rival
        # lines killed independently (§4), so a cross-branch edge would couple
        # lines that must stay separable.
        cited = get_node(session, cited_node_id)
        if cited.branch_id != node.branch_id:
            raise CitationTargetError(
                "a node may only cite another node in the SAME branch "
                "(cross-branch citation would couple rival lines)"
            )
        cited_node_uuid = cited.id
    else:
        # The inward reference to a real decision. No FK carries this (the
        # inward-only invariant is structural); the decision's existence is
        # validated against the real `decisions` table here. A reverse
        # (real→shadow) citation is impossible — there is no such direction.
        cited_decision_uuid = _parse_uuid(
            cited_decision_id, CitationTargetError, "decision id"
        )
        if session.get(Decision, cited_decision_uuid) is None:
            raise CitationTargetError(
                f"no real decision with id {cited_decision_id!r}"
            )

    citation = ShadowNodeCitation(
        node_id=node.id,
        cited_node_id=cited_node_uuid,
        cited_decision_id=cited_decision_uuid,
    )
    session.add(citation)
    session.flush()
    replication_stream.emit(
        session,
        EVENT_SHADOW_CITATION_ADDED,
        replication_stream.citation_added_payload(citation),
    )
    return _citation_dict(citation)


def list_citations(session: Session, node_id: str) -> list[dict]:
    """The citations a node makes (its outgoing inward references), oldest
    first — the dependency set a future graduation's cherry-pick closure walks."""
    node = get_node(session, node_id)
    rows = session.scalars(
        select(ShadowNodeCitation)
        .where(ShadowNodeCitation.node_id == node.id)
        .order_by(ShadowNodeCitation.created_at, ShadowNodeCitation.id)
    )
    return [_citation_dict(c) for c in rows]


# --- conversation (spec §2/§5) ---------------------------------------------


def add_message(
    session: Session,
    branch_id: uuid.UUID | str,
    markdown: str,
    author: str,
) -> dict:
    """Append a `message` event to a branch's durable conversation log (spec §2).

    `author` must be `human` (the UI composer's browser seam) or `agent` (an
    MCP session or the turn-runner logging a turn) — anything else raises
    `MessageValidationError`, so the thread renderer's you/agent mapping can't
    be fed a third value. The payload is `{"author": author, "markdown":
    markdown}`. `markdown` must be non-blank and ≤ `UI_WRITE_BODY_LIMIT` UTF-8
    bytes — NOTE this caps the markdown FIELD, while the /ui-api proxy caps the
    whole JSON body at the same constant, so the effective browser-path ceiling
    is a few bytes lower (the JSON envelope); this service cap is the MCP
    path's boundary and the browser path's backstop. Violations raise
    `MessageValidationError` (a `ValueError`, → 422 at the route);
    unknown/malformed `branch_id` raises `BranchNotFoundError` (→ 404); an
    archived branch raises `BranchArchivedError` (→ 409). Returns the appended
    event (carrying its `seq` — a write receipt)."""
    if author not in CONVERSATION_AUTHORS:
        raise MessageValidationError(
            f"author must be one of {sorted(CONVERSATION_AUTHORS)!r}, "
            f"got {author!r}"
        )
    if not isinstance(markdown, str) or not markdown.strip():
        raise MessageValidationError("message markdown must be a non-empty string")
    if len(markdown.encode("utf-8")) > UI_WRITE_BODY_LIMIT:
        raise MessageValidationError(
            f"message markdown exceeds {UI_WRITE_BODY_LIMIT} bytes"
        )
    return _append_conversation_event(
        session,
        branch_id,
        CONVERSATION_MESSAGE_KIND,
        {"author": author, "markdown": markdown},
    )


def append_error(
    session: Session,
    branch_id: uuid.UUID | str,
    error: str,
) -> dict:
    """Append an `agent.error` event (spec §2) — a failed/timed-out agent turn
    made VISIBLE in the thread (fail-visible, ui-shell §4.4), never a silent
    stall. Shipped alongside its two readers (`_conversation_tail_dict` and the
    thread merge) so the payload key can't drift when the turn-runner (#71)
    starts writing it: the payload is `{"error": error}`. Same 404/409
    semantics as `add_message` (an archived branch takes no further turns)."""
    error = (error or "").strip() or "agent turn failed (no detail)"
    return _append_conversation_event(
        session, branch_id, CONVERSATION_ERROR_KIND, {"error": error}
    )


def _append_conversation_event(
    session: Session,
    branch_id: uuid.UUID | str,
    kind: str,
    payload: dict,
) -> dict:
    """The ONE conversation-log appender — every event kind allocates `seq`
    through this lock, so there is exactly one allocator to reason about
    (a second copy could drift and hand out duplicate seqs under concurrency).

    `seq` allocation (spec §2): the branch row is `SELECT ... FOR UPDATE`-locked
    (the natural serialization point — turns are rare, no advisory-lock
    machinery), then `max(seq)+1` scoped to this branch. The archived check
    sits UNDER the lock, so a concurrent `archive_branch` can't slip an event
    in (no TOCTOU). Locking the BRANCH row (not the event rows) works even for
    the first event, when no event row exists to lock; the DB unique
    `(branch_id, seq)` is the backstop."""
    bid = _parse_uuid(branch_id, BranchNotFoundError, "branch id")
    branch = session.scalar(
        select(ShadowBranch).where(ShadowBranch.id == bid).with_for_update()
    )
    if branch is None:
        raise BranchNotFoundError(f"no shadow branch with id {branch_id!r}")
    if branch.status == SHADOW_BRANCH_STATUS_ARCHIVED:
        raise BranchArchivedError(
            f"shadow branch {branch.scope_slug}:{branch.name!r} is archived"
        )

    next_seq = (
        session.scalar(
            select(func.max(ShadowConversationEvent.seq)).where(
                ShadowConversationEvent.branch_id == bid
            )
        )
        or 0
    ) + 1
    event = ShadowConversationEvent(
        branch_id=bid, seq=next_seq, kind=kind, payload=payload
    )
    session.add(event)
    session.flush()
    # STREAM emit — one event type for the ONE appender (message + agent.error
    # both come through here). The payload omits `seq` (locally re-allocated at
    # apply; see replication_stream's docstring).
    replication_stream.emit(
        session,
        EVENT_SHADOW_CONVERSATION_APPENDED,
        replication_stream.conversation_appended_payload(event),
    )
    return _conversation_event_dict(event)


# --- shadow-scoped corpus search (spec §5) ---------------------------------
#
# The speculation surface's discovery read: full-text search across shadow
# content — branches (active AND archived), nodes, narrative-notes — bundled with
# the REAL decisions backlinked to a shadow line
# (`Decision.shadow_origin_label IS NOT NULL`). A SEPARATE surface from any real
# search (governance has no real `search`); the attached decisions ARE the
# inward-only overlap (shadow cites real). It uses Postgres inline FTS
# (`websearch_to_tsquery` + `ts_rank` + `ts_headline`), no stored FTS column.


def corpus_search(
    session: Session,
    query: str,
    scope_id: uuid.UUID | str | None = None,
    scope_slug: str | None = None,
    limit: int | None = None,
) -> dict:
    """Shadow-scoped full-text search (spec §5) over three corpora: shadow
    branches (name + narrative-notes, BOTH active and archived — that breadth is
    the point), shadow nodes (statement + rationale), and the REAL decisions
    backlinked to a shadow line (`shadow_origin_label IS NOT NULL`). Ranked
    headers merged across corpora, `{kind, id, scope, snippet, rank}`.

    When `scope_id` is given (the caller resolved the slug against the platform)
    each corpus narrows to that EXACT scope (branch.scope_id / node→branch.scope_id
    / decision.scope_id); omitted, it searches all shadow content. Raises
    `ValueError` on a blank query. (The monolith narrowed to the scope SUBTREE via
    its in-process scope tree — which is the platform's now; an exact-scope
    narrowing keeps this import-pure.)"""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("`query` must be a non-empty string")

    sid = None
    if scope_id is not None:
        sid = (
            scope_id if isinstance(scope_id, uuid.UUID)
            else uuid.UUID(str(scope_id))
        )

    lim = _resolve_list_limit(
        limit, CORPUS_SEARCH_DEFAULT_LIMIT, CORPUS_SEARCH_MAX_LIMIT
    )

    # The query is ALWAYS a bound parameter (func args bind), never interpolated.
    tsq = func.websearch_to_tsquery(_TS_CONFIG, query)

    rows: list[dict] = []
    total = 0
    for kind in _CORPUS_KIND_ORDER:
        text, id_col, scope_col, count_from, joins, filters = _corpus_search_spec(
            kind, sid
        )
        vec = func.to_tsvector(_TS_CONFIG, text)
        match = vec.op("@@")(tsq)
        rank = func.ts_rank(vec, tsq)
        snippet = func.ts_headline(_TS_CONFIG, text, tsq)
        stmt = select(id_col, scope_col, snippet, rank)
        for join in joins:
            stmt = stmt.join(*join)
        stmt = (
            stmt.where(match, *filters)
            .order_by(rank.desc(), id_col.asc())
            .limit(lim)  # top-lim per kind ⊇ the merged top-lim
        )
        rows.extend(
            {"kind": kind,
             "id": str(rid),
             "scope": rscope,
             "snippet": rsnippet,
             "rank": float(rrank)}
            for rid, rscope, rsnippet, rrank in session.execute(stmt)
        )
        cstmt = select(func.count()).select_from(count_from)
        for join in joins:
            cstmt = cstmt.join(*join)
        total += session.scalar(cstmt.where(match, *filters)) or 0

    order = {k: i for i, k in enumerate(_CORPUS_KIND_ORDER)}
    rows.sort(key=lambda r: (-r["rank"], order[r["kind"]], r["id"]))
    return {
        "query": query,
        "scope": scope_slug if sid is not None else None,
        "kinds": list(_CORPUS_KIND_ORDER),
        "results": rows[:lim],
        "results_total": total,
    }


def _corpus_search_spec(kind: str, scope_id: uuid.UUID | None):
    """One corpus = (text expr, id col, scope expr, count-from model, join list,
    narrowing filters). `concat_ws` skips NULLs so an optional column never nulls
    the vector. `scope_id` is None for an unscoped (all-shadow) search; otherwise
    each corpus narrows to that exact scope on its scope-bearing column. The scope
    label comes from the denormalized `scope_slug` (branches) / the row's own
    `scope_slug` (decisions) — no scope-table join (scopes are platform-owned)."""
    if kind == "shadow_branch":
        # Active AND archived — NO status filter (that breadth is the point §5).
        text = func.concat_ws(
            " ", ShadowBranch.name, ShadowBranch.narrative_notes
        )
        filters = (
            [] if scope_id is None else [ShadowBranch.scope_id == scope_id]
        )
        return (
            text, ShadowBranch.id, ShadowBranch.scope_slug, ShadowBranch,
            [], filters,
        )
    if kind == "shadow_node":
        # Scope rides through the node's branch (nodes carry no scope of their
        # own — the branch is anchored at the cradle scope §4).
        text = func.concat_ws(
            " ", ShadowNode.statement, ShadowNode.rationale
        )
        filters = (
            [] if scope_id is None else [ShadowBranch.scope_id == scope_id]
        )
        return (
            text, ShadowNode.id, ShadowBranch.scope_slug, ShadowNode,
            [(ShadowBranch, ShadowBranch.id == ShadowNode.branch_id)],
            filters,
        )
    if kind == "decision":
        # The REAL decisions backlinked to a shadow line (the inward overlap),
        # gated to those (`shadow_origin_label IS NOT NULL`).
        text = func.concat_ws(" ", Decision.decision, Decision.rationale)
        filters = [Decision.shadow_origin_label.is_not(None)]
        if scope_id is not None:
            filters.append(Decision.scope_id == scope_id)
        return (
            text, Decision.id, Decision.scope_slug, Decision, [], filters,
        )
    raise AssertionError(f"unhandled corpus kind {kind!r}")  # caller-guarded
