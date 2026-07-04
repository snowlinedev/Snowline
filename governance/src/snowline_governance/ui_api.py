"""The governance plugin's `/ui-api` routes — the FIRST registered UI
contribution (ui-shell.md §8 step 3, issue #55): read-only JSON views over the
EXISTING shadow service layer (`shadow.py`), fed to the platform's declarative
UI shell (§3/§4). No new write path — every handler here is a read, and the
manifest `ui` block (`registration.build_manifest`) is what tells the shell
these routes exist.

Route/param shape: a branch is addressed by its STABLE `id` (a UUID), not
`<scope>:<name>`. Branch names are only unique WITHIN a scope (spec §4), so a
scope-qualified route param would need two path segments (`/shadow/{scope}/
{name}`, with `scope` itself containing a `/` for `owner/repo` scopes — awkward
to template into a single `{name}`-style segment) or percent-encoding tricks.
The `id` is already the row's stable primary key, already serialized by
`shadow._branch_dict`, and round-trips unambiguously with a single path
segment — so the table page links `/shadow/{branch_id}` and the thread page's
`data` is keyed the same way: `/ui-api/pages/branches/{branch_id}`.

Contract fidelity (dashboard/src/kinds/kinds.tsx's validators, ui-shell.md
§4.1/§4.2):
  - `stat`: `{ value, label }` — the widget's `value` is the open-branch count.
  - `table`: `{ columns: [{key, label}], rows: [{cells, href?}], empty }`.
  - `thread`: `{ title, meta, nodes: [{author, kind, markdown, at, citations?}] }`.

The one shape decision worth flagging: `shadow.py` has no single "all branches
across all scopes" read (`list_branches` takes a resolved scope; `get_branch`
takes `<scope>:<name>`) — both are scope-first because the shadow surface is
reached through a resolved scope. The UI's stat + table need a cross-scope
view (every branch, or an open-branch count, regardless of scope), so this
module queries `ShadowBranch` directly rather than stretching `shadow.py`'s
scope-first functions to a shape they were not designed for. It DOES reuse
`shadow.py`'s node/citation serializers and its branch-nodes/citations lookups
(`shadow._branch_nodes`, `shadow.list_citations`) rather than re-deriving them.

DB access follows the same anyio.to_thread + session_scope pattern the MCP
surface's handlers use (`mcp_surface.py`): a sync function does the DB work,
awaited off the event loop from an async FastAPI route.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import anyio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from snowline_governance import concurrence, shadow
from snowline_governance.db import session_scope
from snowline_governance.models import (
    DEFAULT_SHADOW_BRANCH_STATUS,
    SHADOW_BRANCH_STATUS_ARCHIVED,
    ShadowBranch,
    ShadowNode,
)

# A sort sentinel for entries whose timestamp is somehow NULL (should not happen
# post-commit) — keeps the chronological merge total-orderable without a None
# comparison blowing up.
_MIN_DT = datetime.min

router = APIRouter(prefix="/ui-api")


# --- widget: stat ------------------------------------------------------------


def _shadow_activity_sync() -> dict:
    with session_scope() as session:
        count = session.scalar(
            select(func.count())
            .select_from(ShadowBranch)
            .where(ShadowBranch.status == DEFAULT_SHADOW_BRANCH_STATUS)
        )
    return {"value": count or 0, "label": "open shadow branches"}


@router.get("/widgets/shadow-activity")
async def shadow_activity_widget() -> dict:
    """`stat` contract (§4.1): the count of open (non-archived) shadow
    branches across every scope — the home-grid activity pulse."""
    return await anyio.to_thread.run_sync(_shadow_activity_sync)


def _unreconciled_widget_sync() -> dict:
    with session_scope() as session:
        count = concurrence.unreconciled_count(session)
    return {"value": count, "label": "unreconciled decisions"}


@router.get("/widgets/unreconciled-decisions")
async def unreconciled_decisions_widget() -> dict:
    """`stat` contract (§4.1): the count of OPEN §6.1 concurrent-sibling pairs
    (replication-continuity — decisions authored on both instances during a
    partition, in overlapping scope, with no supersession between them yet).
    Zero is the healthy standing state; anything else is first-class work the
    daily-driver should see in the flow (the `unreconciled_decisions` tool
    carries the detail)."""
    return await anyio.to_thread.run_sync(_unreconciled_widget_sync)


# --- page: branches table ------------------------------------------------


def _branch_row(branch: ShadowBranch, node_count: int) -> dict:
    return {
        "cells": {
            "branch": branch.name,
            "scope": branch.scope_slug,
            "status": branch.status,
            "nodes": node_count,
            "updated": branch.updated_at.isoformat() if branch.updated_at else None,
        },
        # Plugin-relative — the shell's row-href handling re-prefixes a
        # leading-'/' href with `/<plugin>` (dashboard/src/kinds/kinds.tsx
        # `prefixHref`), landing on `/governance/shadow/{branch_id}`.
        "href": f"/shadow/{branch.id}",
    }


def _branches_table_sync() -> dict:
    with session_scope() as session:
        branches = list(
            session.scalars(
                select(ShadowBranch).order_by(
                    ShadowBranch.updated_at.desc(), ShadowBranch.id
                )
            )
        )
        rows = []
        for b in branches:
            node_count = (
                session.scalar(
                    select(func.count())
                    .select_from(ShadowNode)
                    .where(ShadowNode.branch_id == b.id)
                )
                or 0
            )
            rows.append(_branch_row(b, node_count))
    return {
        "columns": [
            {"key": "branch", "label": "Branch"},
            {"key": "scope", "label": "Scope"},
            {"key": "status", "label": "Status", "kind": "chip"},
            {"key": "nodes", "label": "Nodes"},
            {"key": "updated", "label": "Updated", "kind": "time"},
        ],
        "rows": rows,
        "empty": "No shadow branches yet.",
    }


@router.get("/pages/branches")
async def branches_table() -> dict:
    """`table` contract (§4.2): every shadow branch, all scopes, newest-updated
    first. Row `href` targets the branch's `thread` page by `id`."""
    return await anyio.to_thread.run_sync(_branches_table_sync)


# --- page: one branch's discussion thread ---------------------------------


def _citation_label(citation: dict) -> str:
    if citation["cited_node_id"] is not None:
        return f"node:{citation['cited_node_id']}"
    return f"decision:{citation['cited_decision_id']}"


def _conversation_entry(event) -> dict:
    """Render a conversation event (shadow-conversations §5) as a thread node.

    A `message` maps its payload author for DISPLAY — `human` → `you`, everything
    else (`agent`) → `agent` — and renders `kind: "message"`. An `agent.error`
    renders `{author: "agent", kind: "error", markdown: <the payload error>}` so
    a failed phase-2 turn is VISIBLE in the thread (fail-visible, §2/ui-shell §4.4)."""
    payload = event.payload or {}
    at = event.created_at.isoformat() if event.created_at else None
    if event.kind == shadow.CONVERSATION_ERROR_KIND:
        return {
            "author": "agent",
            "kind": "error",
            "markdown": payload.get("error", ""),
            "at": at,
        }
    author = "you" if payload.get("author") == "human" else "agent"
    return {
        "author": author,
        "kind": "message",
        "markdown": payload.get("markdown", ""),
        "at": at,
    }


def _branch_thread_sync(branch_id: str) -> dict:
    try:
        bid = uuid.UUID(branch_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="no such shadow branch") from None

    with session_scope() as session:
        branch = session.get(ShadowBranch, bid)
        if branch is None:
            raise HTTPException(status_code=404, detail="no such shadow branch")

        nodes: list[dict] = []
        # The branch's narrative notes (the running reasoning thread, §4) come
        # FIRST regardless of any timestamp so one page tells the whole story
        # without a second shell kind: a synthetic "narrative"/"notes" node ahead
        # of everything else.
        if branch.narrative_notes:
            nodes.append(
                {
                    "author": "narrative",
                    "kind": "notes",
                    "markdown": branch.narrative_notes,
                    "at": branch.updated_at.isoformat() if branch.updated_at else None,
                }
            )

        # The shadow nodes AND the conversation events (spec §5) INTERLEAVE
        # chronologically by their creation time — one page, one timeline. Python's
        # sort is stable, so a node and a message sharing a timestamp keep their
        # insertion order (nodes before messages) deterministically.
        timeline: list[tuple[datetime, dict]] = []
        for node in shadow._branch_nodes(session, branch.id):
            citations = shadow.list_citations(session, str(node.id))
            markdown = node.statement
            if node.rationale:
                markdown = f"{markdown}\n\n{node.rationale}"
            entry: dict = {
                "author": "shadow",
                "kind": "node",
                "markdown": markdown,
                "at": node.created_at.isoformat() if node.created_at else None,
            }
            if citations:
                entry["citations"] = [_citation_label(c) for c in citations]
            timeline.append((node.created_at or _MIN_DT, entry))

        for event in shadow._branch_conversation_events(session, branch.id):
            timeline.append((event.created_at or _MIN_DT, _conversation_entry(event)))

        timeline.sort(key=lambda item: item[0])
        nodes.extend(entry for _, entry in timeline)

        has_notes = bool(branch.narrative_notes)
        meta = f"{branch.scope_slug} · {branch.status}"
        if has_notes:
            meta = f"{meta} · has narrative notes"

        result: dict = {"title": branch.name, "meta": meta, "nodes": nodes}
        # Machine-readable status flags for the shell (ui-shell §4.2 thread
        # contract, EXTENDED here): a top-level `flags` list. The composer's
        # `disabled_when: "archived"` (registration.build_manifest) keys on the
        # presence of the literal string "archived" in this list — that is the
        # exact contract the dashboard shell (#69) consumes to grey out the
        # composer. Absent/empty when the branch is active.
        if branch.status == SHADOW_BRANCH_STATUS_ARCHIVED:
            result["flags"] = [SHADOW_BRANCH_STATUS_ARCHIVED]
        return result


@router.get("/pages/branches/{branch_id}")
async def branch_thread(branch_id: str) -> dict:
    """`thread` contract (§4.2, EXTENDED by shadow-conversations §5): the branch's
    narrative notes (if any) FIRST, then its shadow nodes and conversation
    messages INTERLEAVED chronologically — the re-entry surface for a speculation
    line. 404s (JSON) on an unknown/malformed `branch_id`.

    Adds a top-level `flags: ["archived"]` list when the branch is archived (the
    chosen shape for the archived status flag; absent otherwise) — the dashboard
    shell's composer `disabled_when: "archived"` greys the composer out by
    checking for `"archived"` in this list."""
    return await anyio.to_thread.run_sync(_branch_thread_sync, branch_id)


# --- page: append a conversation message (shadow-conversations §5) ---------


class _MessageBody(BaseModel):
    """The composer POST body (spec §5): just `{ "markdown": str }`. The author is
    ALWAYS "human" on this route — the browser is the human seam, so a client
    cannot spoof an "agent" message here (that's the MCP `add_message` verb's
    job)."""

    markdown: str


def _branch_add_message_sync(branch_id: str, markdown: str) -> dict:
    with session_scope() as session:
        try:
            return shadow.add_message(session, branch_id, markdown, "human")
        except shadow.BranchNotFoundError:
            raise HTTPException(
                status_code=404, detail="no such shadow branch"
            ) from None
        except shadow.BranchArchivedError:
            raise HTTPException(
                status_code=409, detail="shadow branch is archived"
            ) from None
        except shadow.MessageValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/pages/branches/{branch_id}/messages")
async def branch_add_message(branch_id: str, body: _MessageBody) -> dict:
    """Append a human message to a branch's conversation log (spec §5). Body is
    `{ "markdown": str }`; the author is ALWAYS "human" (the browser seam). Returns
    the appended event (with its `seq`). 404 on an unknown/malformed `branch_id`,
    409 on an archived branch, 422 on blank/oversize markdown (capped at the
    /ui-api proxy's `UI_WRITE_BODY_LIMIT`). Same `anyio.to_thread` + `session_scope`
    pattern as the read routes above."""
    return await anyio.to_thread.run_sync(
        _branch_add_message_sync, branch_id, body.markdown
    )
