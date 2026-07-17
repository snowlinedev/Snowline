"""Shadow / speculation graph behavior — branch/node/citation roundtrips, the
inward-only citation invariant, corpus search, and the KEY isolation property:
the `shadow` MCP surface carries the shadow writes + read-real grounding and ZERO
real-write verbs (that absence IS the isolation, decision 8a7f0a11).

DB-backed tests skip cleanly when Postgres is unavailable; the surface-shape
isolation test needs no DB (it enumerates `list_tools()`).
"""

from __future__ import annotations

import json
import uuid

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from snowline_governance import decisions, shadow
from snowline_governance.db import session_scope
from snowline_governance.mcp_surface import build_main_surface, build_shadow_surface
from snowline_governance.models import Decision


def _sid(slug: str) -> uuid.UUID:
    """The stable per-slug scope id — IDENTICAL to `StubScopeClient`'s."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


class _NoopScopeClient:
    def resolve(self, slug: str): return None
    def ancestors(self, slug: str): return []


# === the shadow surface tool set (the isolation property) — no DB needed =====

# Every real-write verb that MUST be absent from the shadow surface (decision
# 8a7f0a11): a speculation session physically cannot mutate the real graph.
_REAL_WRITE_VERBS = {
    "record_decision",
    "supersede_decision",
    # graduation mints a REAL decision (shadow → real), so it's a real-write verb
    # that MUST live on `main` and be ABSENT from shadow (the principal split,
    # decision 99b92e1d): the shadow agent drafts, the principal on main executes.
    "graduate",
    # branch-level graduation + rejection also mint real decisions (decisions
    # 0c26be5c / be803a2b) — same principal split, so also main-only.
    "graduate_branch",
    "record_branch_rejection",
    "register_artifact",
    "revise_artifact",
    "resolve_artifact",
    "set_governs",
    "set_maturity",
}

_SHADOW_WRITE_TOOLS = {
    "create_branch",
    "list_branches",
    "get_branch",
    "set_narrative_notes",
    "add_node",
    "add_citation",
    "list_citations",
    "shadow_corpus_search",
    # archive_branch is a PURE shadow op (active→archived status flip, no real
    # write), so it lives on the shadow surface alongside the other shadow writes.
    "archive_branch",
    # add_message appends to the branch's durable conversation log (a pure shadow
    # write, shadow-conversations §5) — the SAME service the /ui-api composer calls.
    "add_message",
}

_READ_REAL_GROUNDING = {
    "get_decision",
    "list_decisions",
    "applicable_decisions",
    "get_artifact",
    "get_artifact_version",
    "list_artifacts",
    "applicable_artifacts",
    # §6.1 unreconciled view (replication-continuity, #79) — a pure read on the
    # shared read set, so speculation sessions see flagged pairs too.
    "unreconciled_decisions",
}


def _tool_names(surface) -> set[str]:
    return {t.name for t in anyio.run(surface.list_tools)}


def test_shadow_surface_is_isolated_no_real_write():
    """THE KEY isolation test: the shadow surface's tool set is EXACTLY the 10
    shadow writes + the 8 read-real grounding tools — and contains ZERO real-write
    verbs. The absence of record_decision / supersede / register / revise /
    resolve / set_governs / set_maturity IS the isolation (decision 8a7f0a11)."""
    surface = build_shadow_surface(scope_client=_NoopScopeClient())
    tools = _tool_names(surface)

    # The shadow writes + read-real grounding are all present.
    assert _SHADOW_WRITE_TOOLS <= tools
    assert _READ_REAL_GROUNDING <= tools
    # And NOTHING else — the surface is exactly these two sets.
    assert tools == _SHADOW_WRITE_TOOLS | _READ_REAL_GROUNDING
    # The load-bearing assertion: every real-write verb is ABSENT.
    assert _REAL_WRITE_VERBS.isdisjoint(tools)
    for verb in _REAL_WRITE_VERBS:
        assert verb not in tools


def test_main_surface_keeps_the_real_write_verbs():
    """The contrapositive: the real-write verbs DO live on the `main` surface (so
    the isolation is a real split, not the tools simply not existing)."""
    surface = build_main_surface(scope_client=_NoopScopeClient())
    tools = _tool_names(surface)
    assert _REAL_WRITE_VERBS <= tools
    # The read-real grounding is shared (registered on both surfaces).
    assert _READ_REAL_GROUNDING <= tools
    # The shadow WRITE verbs are NOT on the main surface.
    assert _SHADOW_WRITE_TOOLS.isdisjoint(tools)


# === branch / node / citation roundtrips =====================================


def test_branch_roundtrip(db_session):
    slug = "acme/widget"
    created = shadow.create_branch(
        db_session, slug, _sid(slug), "auth-as-jwt", "exploring jwt"
    )
    assert created["scope"] == slug
    assert created["name"] == "auth-as-jwt"
    assert created["address"] == "acme/widget:auth-as-jwt"
    assert created["status"] == "active"
    assert created["nodes"] == []

    got = shadow.get_branch(db_session, slug, "auth-as-jwt")
    assert got["id"] == created["id"]

    # #134/#139: scope input is case-insensitive at the branch-address seam
    # too — the same mixed-case input that can CREATE (via platform resolve)
    # must also ADDRESS the branch.
    cased = shadow.get_branch(db_session, "Acme/Widget", "auth-as-jwt")
    assert cased["id"] == created["id"]
    assert got["narrative_notes"] == "exploring jwt"

    listed = shadow.list_branches(db_session, slug, _sid(slug))
    assert [b["id"] for b in listed] == [created["id"]]


def test_duplicate_branch_rejected(db_session):
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    with pytest.raises(shadow.DuplicateBranchError):
        shadow.create_branch(db_session, slug, _sid(slug), "line-a")


def test_set_narrative_notes(db_session):
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    updated = shadow.set_narrative_notes(
        db_session, slug, "line-a", "we decided to pivot"
    )
    assert updated["narrative_notes"] == "we decided to pivot"
    assert shadow.get_branch(db_session, slug, "line-a")["narrative_notes"] == (
        "we decided to pivot"
    )


def test_node_roundtrip(db_session):
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    node = shadow.add_node(
        db_session, slug, "line-a", "use rs256", "asymmetric keys"
    )
    assert node["statement"] == "use rs256"
    assert node["rationale"] == "asymmetric keys"
    assert node["graduated_decision_id"] is None

    branch = shadow.get_branch(db_session, slug, "line-a")
    assert [n["id"] for n in branch["nodes"]] == [node["id"]]


def test_branch_not_found(db_session):
    with pytest.raises(shadow.BranchNotFoundError):
        shadow.get_branch(db_session, "acme/widget", "nope")


# === the citation invariant (inward-only) ====================================


def test_citation_node_to_node_same_branch(db_session):
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    n1 = shadow.add_node(db_session, slug, "line-a", "decision 1")
    n2 = shadow.add_node(db_session, slug, "line-a", "decision 2")
    cit = shadow.add_citation(db_session, n2["id"], cited_node_id=n1["id"])
    assert cit["cited_node_id"] == n1["id"]
    assert cit["cited_decision_id"] is None
    assert [c["id"] for c in shadow.list_citations(db_session, n2["id"])] == [
        cit["id"]
    ]


def test_citation_node_to_real_decision(db_session):
    """A node may cite a REAL decision — the permitted inward reference. The real
    decision's id is stored as a plain value; existence is validated here."""
    slug = "acme/widget"
    real = decisions.record_decision(
        db_session, slug, _sid(slug), "use postgres", "solid"
    )
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    n1 = shadow.add_node(db_session, slug, "line-a", "build on top of pg")
    cit = shadow.add_citation(
        db_session, n1["id"], cited_decision_id=real["id"]
    )
    assert cit["cited_decision_id"] == real["id"]
    assert cit["cited_node_id"] is None


def test_citation_cross_branch_rejected(db_session):
    """A node may NOT cite a node in a DIFFERENT branch — that would couple rival
    lines that must stay separable."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    shadow.create_branch(db_session, slug, _sid(slug), "line-b")
    n_a = shadow.add_node(db_session, slug, "line-a", "a1")
    n_b = shadow.add_node(db_session, slug, "line-b", "b1")
    with pytest.raises(shadow.CitationTargetError):
        shadow.add_citation(db_session, n_a["id"], cited_node_id=n_b["id"])


def test_citation_to_nonexistent_real_decision_rejected(db_session):
    """Citing a real decision that does not exist raises — the inward target must
    exist (validated at the service layer, since there is no FK)."""
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    n1 = shadow.add_node(db_session, slug, "line-a", "n1")
    with pytest.raises(shadow.CitationTargetError):
        shadow.add_citation(
            db_session, n1["id"], cited_decision_id=str(uuid.uuid4())
        )


def test_citation_requires_exactly_one_target(db_session):
    slug = "acme/widget"
    shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    n1 = shadow.add_node(db_session, slug, "line-a", "n1")
    # Zero targets.
    with pytest.raises(shadow.CitationTargetError):
        shadow.add_citation(db_session, n1["id"])
    # Two targets.
    other = shadow.add_node(db_session, slug, "line-a", "n2")
    real = decisions.record_decision(db_session, slug, _sid(slug), "d")
    with pytest.raises(shadow.CitationTargetError):
        shadow.add_citation(
            db_session,
            n1["id"],
            cited_node_id=other["id"],
            cited_decision_id=real["id"],
        )


# === corpus search ============================================================


def test_corpus_search_finds_branch_node_and_decision(db_session):
    slug = "acme/widget"
    # A real decision backlinked to a shadow line (shadow_origin_label set) is
    # part of the corpus.
    real = decisions.record_decision(
        db_session, slug, _sid(slug), "adopt elephant database", "graduated"
    )
    real_row = db_session.get(Decision, uuid.UUID(real["id"]))
    real_row.shadow_origin_label = "acme/widget:elephant"
    db_session.flush()

    shadow.create_branch(
        db_session, slug, _sid(slug), "elephant", "explore elephant storage"
    )
    shadow.add_node(
        db_session, slug, "elephant", "store on elephant", "scales well"
    )

    res = shadow.corpus_search(db_session, "elephant")
    kinds = {r["kind"] for r in res["results"]}
    assert "shadow_branch" in kinds
    assert "shadow_node" in kinds
    assert "decision" in kinds
    assert res["results_total"] >= 3


def test_corpus_search_scope_narrowing(db_session):
    slug_a = "acme/widget"
    slug_b = "acme/gadget"
    shadow.create_branch(db_session, slug_a, _sid(slug_a), "zebra", "zebra notes")
    shadow.create_branch(db_session, slug_b, _sid(slug_b), "zebra2", "zebra notes")

    scoped = shadow.corpus_search(
        db_session, "zebra", scope_id=_sid(slug_a), scope_slug=slug_a
    )
    # Only the branch anchored at acme/widget matches the exact-scope narrowing.
    branch_results = [r for r in scoped["results"] if r["kind"] == "shadow_branch"]
    assert len(branch_results) == 1
    assert scoped["scope"] == slug_a


def test_corpus_search_rejects_blank_query(db_session):
    with pytest.raises(ValueError):
        shadow.corpus_search(db_session, "   ")


# === conversation (shadow-conversations §2/§5) ===============================


def test_add_message_allocates_monotonic_seq(db_session):
    slug = "acme/widget"
    branch = shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    bid = branch["id"]

    e1 = shadow.add_message(db_session, bid, "first", "human")
    e2 = shadow.add_message(db_session, bid, "second", "agent")

    # Per-branch monotonic seq (spec §2): 1, then 2.
    assert e1["seq"] == 1
    assert e2["seq"] == 2
    assert e1["kind"] == "message"
    assert e1["payload"] == {"author": "human", "markdown": "first"}
    assert e2["payload"] == {"author": "agent", "markdown": "second"}
    assert e1["created_at"] and e2["created_at"]


def test_add_message_seq_is_per_branch(db_session):
    slug = "acme/widget"
    b1 = shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    b2 = shadow.create_branch(db_session, slug, _sid(slug), "line-b")

    # Each branch has its OWN seq counter (scoped by branch_id, spec §2).
    assert shadow.add_message(db_session, b1["id"], "a1", "human")["seq"] == 1
    assert shadow.add_message(db_session, b2["id"], "b1", "human")["seq"] == 1
    assert shadow.add_message(db_session, b1["id"], "a2", "human")["seq"] == 2


def test_add_message_for_update_path_yields_sequential_seqs(db_session):
    # The FOR UPDATE-locked max(seq)+1 allocation (spec §2 concurrency safety):
    # a burst of appends on one branch hands out a gapless, ordered seq run and
    # the DB's unique (branch_id, seq) is never violated. True cross-connection
    # concurrency isn't practical in this single-session harness, so this
    # exercises the lock/allocate code path and pins the sequential outcome.
    slug = "acme/widget"
    branch = shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    seqs = [
        shadow.add_message(db_session, branch["id"], f"m{i}", "human")["seq"]
        for i in range(5)
    ]
    assert seqs == [1, 2, 3, 4, 5]


def test_add_message_rejects_blank_markdown(db_session):
    slug = "acme/widget"
    branch = shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    with pytest.raises(shadow.MessageValidationError):
        shadow.add_message(db_session, branch["id"], "   ", "human")


def test_add_message_rejects_oversize_markdown(db_session):
    from snowline_plugin_sdk.ui import UI_WRITE_BODY_LIMIT

    slug = "acme/widget"
    branch = shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    # One byte over the proxy's UTF-8 byte cap.
    oversize = "x" * (UI_WRITE_BODY_LIMIT + 1)
    with pytest.raises(shadow.MessageValidationError):
        shadow.add_message(db_session, branch["id"], oversize, "human")
    # A message exactly AT the cap is accepted (boundary is inclusive).
    at_cap = "y" * UI_WRITE_BODY_LIMIT
    assert shadow.add_message(db_session, branch["id"], at_cap, "human")["seq"] == 1


def test_add_message_unknown_branch_raises_not_found(db_session):
    with pytest.raises(shadow.BranchNotFoundError):
        shadow.add_message(db_session, str(uuid.uuid4()), "hi", "human")


def test_add_message_malformed_branch_id_raises_not_found(db_session):
    with pytest.raises(shadow.BranchNotFoundError):
        shadow.add_message(db_session, "not-a-uuid", "hi", "human")


def test_add_message_archived_branch_raises(db_session):
    slug = "acme/widget"
    branch = shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    shadow.archive_branch(db_session, slug, "line-a")
    with pytest.raises(shadow.BranchArchivedError):
        shadow.add_message(db_session, branch["id"], "too late", "human")


def test_get_branch_includes_conversation_tail(db_session):
    slug = "acme/widget"
    branch = shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    shadow.add_message(db_session, branch["id"], "human says hi", "human")
    shadow.add_message(db_session, branch["id"], "agent replies", "agent")

    got = shadow.get_branch(db_session, slug, "line-a")
    tail = got["conversation"]
    # Oldest-first, each {seq, author, markdown, at} (author kept RAW for MCP).
    assert [e["seq"] for e in tail] == [1, 2]
    assert tail[0] == {
        "seq": 1,
        "author": "human",
        "markdown": "human says hi",
        "at": tail[0]["at"],
    }
    assert tail[1]["author"] == "agent"
    assert tail[1]["markdown"] == "agent replies"


def test_get_branch_conversation_tail_caps_and_includes_errors(db_session):
    slug = "acme/widget"
    branch = shadow.create_branch(db_session, slug, _sid(slug), "line-a")
    # More than the tail cap, so the OLDEST drop and only the last N remain.
    total = shadow.CONVERSATION_TAIL_LIMIT + 3
    for i in range(total):
        shadow.add_message(db_session, branch["id"], f"m{i}", "human")
    # An agent.error event counts as conversation (spec §2) and appears in the tail.
    shadow.append_error(db_session, branch["id"], "boom")

    tail = shadow.get_branch(db_session, slug, "line-a")["conversation"]
    assert len(tail) == shadow.CONVERSATION_TAIL_LIMIT
    # Newest entries retained; the error is the very last, rendered as an agent.
    last = tail[-1]
    assert last["author"] == "agent"
    assert last["markdown"] == "boom"
    assert last["seq"] == total + 1


def test_append_error_allocates_seq_and_respects_archive(db_session):
    # append_error shares the ONE seq allocator with add_message — interleaved
    # appends of both kinds get strictly increasing seqs — and an archived
    # branch takes no further error events either.
    slug = "acme/widget"
    branch = shadow.create_branch(db_session, slug, _sid(slug), "line-b")
    m1 = shadow.add_message(db_session, branch["id"], "hi", "human")
    e1 = shadow.append_error(db_session, branch["id"], "codex timed out")
    m2 = shadow.add_message(db_session, branch["id"], "retry?", "human")
    assert [m1["seq"], e1["seq"], m2["seq"]] == [1, 2, 3]
    assert e1["kind"] == shadow.CONVERSATION_ERROR_KIND
    assert e1["payload"] == {"error": "codex timed out"}
    # Blank error text still yields a visible message, never an empty bubble.
    e2 = shadow.append_error(db_session, branch["id"], "   ")
    assert e2["payload"]["error"]
    shadow.archive_branch(db_session, slug, "line-b")
    with pytest.raises(shadow.BranchArchivedError):
        shadow.append_error(db_session, branch["id"], "late failure")


def test_add_message_rejects_unknown_author(db_session):
    # The author enum guards the thread renderer's you/agent mapping — an MCP
    # caller can't invent a third author kind (or spoof arbitrary strings).
    slug = "acme/widget"
    branch = shadow.create_branch(db_session, slug, _sid(slug), "line-c")
    with pytest.raises(shadow.MessageValidationError, match="author"):
        shadow.add_message(db_session, branch["id"], "hi", "narrative")


# --- MCP parity (shadow-conversations §5) — in-memory FastMCP round-trip ------


def _shadow_tool(tool_name: str, args: dict) -> dict:
    """Call one `shadow` MCP tool over the in-memory transport (mirroring the
    gateway's `create_connected_server_and_client_session` pattern) and return the
    tool's structured dict result. The surface opens its OWN `session_scope`, so a
    caller must COMMIT the branch/messages first (via `session_scope()`)."""

    async def _run() -> dict:
        surface = build_shadow_surface(scope_client=_NoopScopeClient())
        async with create_connected_server_and_client_session(
            surface, raise_exceptions=True
        ) as session:
            result = await session.call_tool(tool_name, args)
            assert result.isError is not True
            return json.loads(result.content[0].text)

    return anyio.run(_run)


def test_mcp_add_message_round_trip(clean_db):
    slug = "acme/widget"
    with session_scope() as s:
        shadow.create_branch(s, slug, _sid(slug), "line-a")

    # Default author is "agent" for MCP callers.
    ev = _shadow_tool(
        "add_message", {"scope": slug, "name": "line-a", "markdown": "from a session"}
    )
    assert ev["seq"] == 1
    assert ev["kind"] == "message"
    assert ev["payload"] == {"author": "agent", "markdown": "from a session"}

    # An explicit "human" author is accepted too.
    ev2 = _shadow_tool(
        "add_message",
        {"scope": slug, "name": "line-a", "markdown": "as human", "author": "human"},
    )
    assert ev2["seq"] == 2
    assert ev2["payload"]["author"] == "human"

    # And get_branch's tail reflects both, oldest-first — the SAME conversation a
    # UI session sees (acceptance §7.6).
    branch = _shadow_tool("get_branch", {"scope": slug, "name": "line-a"})
    assert [e["seq"] for e in branch["conversation"]] == [1, 2]
    assert branch["conversation"][0]["author"] == "agent"
    assert branch["conversation"][1]["author"] == "human"


def test_mcp_get_branch_conversation_tail(clean_db):
    slug = "acme/widget"
    with session_scope() as s:
        branch = shadow.create_branch(s, slug, _sid(slug), "line-a")
        shadow.add_message(s, branch["id"], "logged in the UI", "human")

    got = _shadow_tool("get_branch", {"scope": slug, "name": "line-a"})
    assert got["conversation"] == [
        {
            "seq": 1,
            "author": "human",
            "markdown": "logged in the UI",
            "at": got["conversation"][0]["at"],
        }
    ]
