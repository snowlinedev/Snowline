"""The shadow turn-runner (turns.py, shadow-conversations §6, issue #71).

Covers, with NO real codex ever invoked (the inference seam `_invoke_codex` is
monkeypatched):
  - the pending-turn query (human-last → pending; agent-last / error-last /
    archived / empty → NOT pending),
  - claim staleness (`_claimable`),
  - the budget-clamp DROP ORDER (conversation → citations → rationales),
  - the prompt template (preamble + narrative + nodes + decisions + human msg),
  - the turn path with a FAKE runner (success → agent message; failure →
    agent.error; archived mid-turn → dropped),
  - loop gating (disabled → returns immediately).

DB-backed tests use the direct `session_scope()` seeding pattern (like
test_ui_api) so a fresh session in `process_turn` sees committed data —
production's cross-connection shape. Pure-logic tests need no DB.
"""

from __future__ import annotations

import uuid

import anyio
import pytest
from sqlalchemy import select

from snowline_governance import shadow, turns
from snowline_governance.db import session_scope
from snowline_governance.models import ShadowConversationEvent
from snowline_governance.shadow import (
    CONVERSATION_ERROR_KIND,
    CONVERSATION_MESSAGE_KIND,
)


def _sid(slug: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


class _NoopScopeClient:
    """No ancestors → applicable_decisions resolves to empty (no live platform)."""

    def resolve(self, slug: str): return None
    def ancestors(self, slug: str): return []


# === pending-turn query ======================================================


def test_find_pending_branches(db_session):
    slug = "acme/widget"
    sid = _sid(slug)

    # human-last → PENDING
    b_pending = shadow.create_branch(db_session, slug, sid, "human-last")
    shadow.add_message(db_session, b_pending["id"], "what about jwt?", "human")

    # agent-last → answered, NOT pending
    b_agent = shadow.create_branch(db_session, slug, sid, "agent-last")
    shadow.add_message(db_session, b_agent["id"], "what about jwt?", "human")
    shadow.add_message(db_session, b_agent["id"], "jwt is fine", "agent")

    # error-last → an agent.error ANSWERS the turn, NOT pending (documented §6)
    b_error = shadow.create_branch(db_session, slug, sid, "error-last")
    shadow.add_message(db_session, b_error["id"], "what about jwt?", "human")
    shadow.append_error(db_session, b_error["id"], "codex fell over")

    # empty (no events) → NOT pending
    shadow.create_branch(db_session, slug, sid, "empty")

    # archived (human-last, but archived) → NOT pending
    b_arch = shadow.create_branch(db_session, slug, sid, "archived")
    shadow.add_message(db_session, b_arch["id"], "still open?", "human")
    shadow.archive_branch(db_session, slug, "archived")

    pending = turns.find_pending_branches(db_session)
    assert {p.name for p in pending} == {"human-last"}
    assert pending[0].scope_slug == slug
    assert str(pending[0].branch_id) == b_pending["id"]


# === claim staleness =========================================================


def test_claimable_unclaimed_and_stale():
    bid = uuid.uuid4()
    claims: dict[uuid.UUID, float] = {}
    timeout = 300.0

    # unclaimed → claimable
    assert turns._claimable(claims, bid, now=1000.0, timeout=timeout)

    # freshly claimed → NOT claimable until the timeout elapses
    claims[bid] = 1000.0
    assert not turns._claimable(claims, bid, now=1000.0 + timeout - 1, timeout=timeout)
    # exactly at the timeout → stale, reclaimable
    assert turns._claimable(claims, bid, now=1000.0 + timeout, timeout=timeout)
    # well past → reclaimable
    assert turns._claimable(claims, bid, now=1000.0 + timeout + 50, timeout=timeout)


# === budget-clamp drop order =================================================


def _clamp_ctx() -> turns.TurnContext:
    """A context whose sections are big enough that the budget forces drops."""
    return turns.TurnContext(
        scope_slug="acme/widget",
        branch_name="line-a",
        narrative_notes="NOTES",
        nodes=[
            {
                "statement": "STMT",
                "rationale": "RATIONALEMARK " + "y" * 2000,
                "cites": ["CITEMARK " + "z" * 2000],
            }
        ],
        history=[
            {"author": "human", "markdown": f"OLDCONV{i} " + "x" * 200}
            for i in range(15)
        ],
        decisions=[],
        decisions_available=True,
        latest_human="LATESTMSG",
    )


def test_clamp_drops_conversation_first_then_citations_then_rationales():
    # Floors: render length with successive sections removed.
    c = _clamp_ctx(); c.history = []
    len_no_conv = len(turns.render_prompt(c))
    c = _clamp_ctx(); c.history = []
    for n in c.nodes:
        n["cites"] = []
    len_no_cite = len(turns.render_prompt(c))
    c = _clamp_ctx(); c.history = []
    for n in c.nodes:
        n["cites"] = []
        n["rationale"] = None
    len_no_rat = len(turns.render_prompt(c))

    assert len_no_conv > len_no_cite > len_no_rat  # each layer is real weight

    # Budget = the "conversation dropped" floor → drops conversation ONLY;
    # citations + rationales survive.
    out = turns.clamp_prompt(_clamp_ctx(), budget=len_no_conv)
    assert "OLDCONV0" not in out  # oldest conversation gone
    assert "CITEMARK" in out       # citations kept
    assert "RATIONALEMARK" in out  # rationales kept
    assert "LATESTMSG" in out      # latest human message NEVER dropped

    # Budget = the "citations dropped" floor → conversation THEN citations drop;
    # rationales survive.
    out = turns.clamp_prompt(_clamp_ctx(), budget=len_no_cite)
    assert "CITEMARK" not in out
    assert "RATIONALEMARK" in out
    assert "LATESTMSG" in out

    # Budget = the "rationales dropped" floor → conversation, citations, THEN
    # rationales drop.
    out = turns.clamp_prompt(_clamp_ctx(), budget=len_no_rat)
    assert "RATIONALEMARK" not in out
    assert "STMT" in out       # node statements never dropped
    assert "LATESTMSG" in out


def test_clamp_preserves_human_message_under_brutal_budget():
    # A scope with huge decisions/statements that blow the budget on their own —
    # the human question is the LAST section, so a naive head-slice would drop
    # it. It MUST survive (dropping decisions, then a tail-preserving truncate).
    ctx = turns.TurnContext(
        scope_slug="acme/widget",
        branch_name="line-a",
        narrative_notes=None,
        nodes=[{"statement": "S" * 500, "rationale": None, "cites": []}],
        history=[],
        decisions=[{"id": f"dec-{i}", "decision": "D" * 500} for i in range(50)],
        decisions_available=True,
        latest_human="HUMAN_QUESTION_MARK",
    )
    out = turns.clamp_prompt(ctx, budget=800)
    assert len(out) <= 800
    assert "HUMAN_QUESTION_MARK" in out  # the question is never truncated away
    assert "dec-49" not in out  # decisions dropped as the backstop


def test_clamp_noop_under_budget():
    ctx = turns.TurnContext(
        scope_slug="s", branch_name="b", narrative_notes=None, latest_human="hi"
    )
    out = turns.clamp_prompt(ctx, budget=turns.PROMPT_CHAR_BUDGET)
    assert out == turns.render_prompt(ctx)


# === the prompt template =====================================================


def test_render_prompt_includes_all_sections():
    ctx = turns.TurnContext(
        scope_slug="acme/widget",
        branch_name="auth-line",
        narrative_notes="NARRATIVE_NOTE_MARK",
        nodes=[{"statement": "NODE_STMT_MARK", "rationale": "why", "cites": []}],
        history=[{"author": "agent", "markdown": "prior turn"}],
        decisions=[{"id": "dec-123", "decision": "DECISION_MARK"}],
        decisions_available=True,
        latest_human="HUMAN_MSG_MARK",
    )
    prompt = turns.render_prompt(ctx)
    assert "speculative co-thinker" in prompt  # the SHADOW-posture preamble
    assert "NARRATIVE_NOTE_MARK" in prompt
    assert "NODE_STMT_MARK" in prompt
    assert "dec-123" in prompt and "DECISION_MARK" in prompt
    assert "prior turn" in prompt
    assert "HUMAN_MSG_MARK" in prompt


def test_render_prompt_notes_missing_platform():
    ctx = turns.TurnContext(
        scope_slug="s",
        branch_name="b",
        narrative_notes=None,
        decisions=[],
        decisions_available=False,
        latest_human="hi",
    )
    assert "platform unreachable" in turns.render_prompt(ctx)


# === the turn path (fake runner) =============================================


def _seed_branch_with_human_msg(slug: str, name: str, markdown: str) -> str:
    with session_scope() as s:
        b = shadow.create_branch(s, slug, _sid(slug), name)
        shadow.add_message(s, b["id"], markdown, "human")
        return b["id"]


def _conversation(slug: str, name: str) -> list[dict]:
    with session_scope() as s:
        return shadow.get_branch(s, slug, name)["conversation"]


def _last_event_kind(branch_id: str) -> str:
    with session_scope() as s:
        ev = s.scalars(
            select(ShadowConversationEvent)
            .where(ShadowConversationEvent.branch_id == uuid.UUID(branch_id))
            .order_by(ShadowConversationEvent.seq.desc())
        ).first()
        return ev.kind


def test_process_turn_success_appends_agent_message(clean_db, monkeypatch):
    slug = "acme/widget"
    bid = _seed_branch_with_human_msg(slug, "line-a", "should we use jwt?")

    monkeypatch.setattr(
        turns, "_invoke_codex",
        lambda prompt, **kw: "Consider RS256 over HS256 — asymmetric keys.",
    )
    turns.process_turn(
        _NoopScopeClient(), branch_id=bid, scope_slug=slug, name="line-a",
        binary="codex", model=None, timeout=5.0,
    )

    conv = _conversation(slug, "line-a")
    assert len(conv) == 2
    assert conv[-1]["author"] == "agent"
    assert conv[-1]["markdown"] == "Consider RS256 over HS256 — asymmetric keys."
    assert _last_event_kind(bid) == CONVERSATION_MESSAGE_KIND


def test_process_turn_failure_appends_agent_error(clean_db, monkeypatch):
    slug = "acme/widget"
    bid = _seed_branch_with_human_msg(slug, "line-b", "should we use jwt?")

    def _boom(prompt, **kw):
        raise turns.TurnError("codex exited 1: boom")

    monkeypatch.setattr(turns, "_invoke_codex", _boom)
    turns.process_turn(
        _NoopScopeClient(), branch_id=bid, scope_slug=slug, name="line-b",
        binary="codex", model=None, timeout=5.0,
    )

    conv = _conversation(slug, "line-b")
    assert len(conv) == 2
    assert conv[-1]["author"] == "agent"  # an error renders as an agent entry
    assert "boom" in conv[-1]["markdown"]
    assert _last_event_kind(bid) == CONVERSATION_ERROR_KIND


def test_process_turn_oversized_reply_becomes_agent_error(clean_db, monkeypatch):
    # A codex reply over the body cap (64 KiB) makes add_message raise
    # MessageValidationError. It must become a fail-visible agent.error (ANSWERED,
    # terminal) — NOT escape and leave the branch pending to hot-loop the turn.
    slug = "acme/widget"
    bid = _seed_branch_with_human_msg(slug, "line-big", "should we use jwt?")

    monkeypatch.setattr(
        turns, "_invoke_codex", lambda prompt, **kw: "x" * (65 * 1024),
    )
    turns.process_turn(
        _NoopScopeClient(), branch_id=bid, scope_slug=slug, name="line-big",
        binary="codex", model=None, timeout=5.0,
    )

    conv = _conversation(slug, "line-big")
    assert len(conv) == 2
    assert conv[-1]["author"] == "agent"
    assert "rejected" in conv[-1]["markdown"]
    assert _last_event_kind(bid) == CONVERSATION_ERROR_KIND  # answered, terminal


def test_process_turn_archived_mid_turn_drops_reply(clean_db, monkeypatch):
    slug = "acme/widget"
    bid = _seed_branch_with_human_msg(slug, "line-c", "still exploring?")
    # Archive AFTER the human message but BEFORE the reply is written.
    with session_scope() as s:
        shadow.archive_branch(s, slug, "line-c")

    monkeypatch.setattr(
        turns, "_invoke_codex", lambda prompt, **kw: "a reply that must be dropped",
    )
    # Must NOT raise — BranchArchivedError is caught and the reply dropped.
    turns.process_turn(
        _NoopScopeClient(), branch_id=bid, scope_slug=slug, name="line-c",
        binary="codex", model=None, timeout=5.0,
    )

    conv = _conversation(slug, "line-c")
    assert len(conv) == 1  # only the original human message; no reply, no error
    assert conv[0]["author"] == "human"


# === loop gating =============================================================


def test_loop_returns_immediately_when_disabled(monkeypatch):
    monkeypatch.delenv("SNOWLINE_SHADOW_TURNS_ENABLED", raising=False)

    async def _run():
        with anyio.fail_after(5):
            await turns.shadow_turn_loop()

    anyio.run(_run)  # returns (does not hang) → the disabled gate works


def test_turns_enabled_parsing(monkeypatch):
    monkeypatch.setenv("SNOWLINE_SHADOW_TURNS_ENABLED", "true")
    assert turns._turns_enabled()
    monkeypatch.setenv("SNOWLINE_SHADOW_TURNS_ENABLED", "0")
    assert not turns._turns_enabled()
    monkeypatch.delenv("SNOWLINE_SHADOW_TURNS_ENABLED", raising=False)
    assert not turns._turns_enabled()
