"""The shadow turn-runner — server-side agent replies on the codex carrier.

Phase 2 of `docs/specs/shadow-conversations.md` (§6). A background loop rides the
governance lifespan task group BESIDE `webhook_delivery_loop` (app.py), notices an
unanswered human message in a shadow branch, assembles the branch's context, runs
ONE inference turn through the codex CLI (the flat-rate ChatGPT-sub carrier,
decision `4bc92633`), and appends the reply to the branch's durable conversation
log. A failed/timed-out turn appends an `agent.error` so the thread SHOWS the
failure (fail-visible, ui-shell §4.4) — never a silent stall.

WHY A LOOP, NOT AN MCP HANDLER: "no LLM call in an MCP handler" (decision
`4bc92633`) — inference lives in a CARRIER. This loop IS that carrier; the codex
subprocess is the ONLY inference seam here. The runner also structurally cannot
touch the real decision graph: it only ever appends conversation EVENTS through
`shadow.add_message` / `shadow.append_error`.

Env knobs (read live, LENIENT-parsed where a bad value could hot-loop or wedge —
they live here, loop-local, mirroring `replication.py`'s `_interval_seconds()`
rather than `config.py`, which holds only the shared DB/platform URLs):

  SNOWLINE_SHADOW_TURNS_ENABLED     — master switch (default false → loop returns).
  SNOWLINE_SHADOW_TURN_POLL_SECONDS — poll cadence (default 5; lenient).
  SNOWLINE_SHADOW_TURN_TIMEOUT      — per-turn hard timeout AND stale-claim age
                                      (default 300; lenient).
  SNOWLINE_SHADOW_TURN_BATCH  — max branches processed per tick (default 1).
  SNOWLINE_SHADOW_TURN_MODEL        — passed to codex `-m` (default unset → omit).
  SNOWLINE_SHADOW_CODEX_BIN         — codex binary name (default "codex"),
                                      resolved via `shutil.which` each poll.
"""

from __future__ import annotations

import functools
import logging
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import anyio
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from snowline_governance import decisions, shadow
from snowline_governance.db import session_scope
from snowline_governance.models import (
    DEFAULT_SHADOW_BRANCH_STATUS,
    ShadowBranch,
    ShadowConversationEvent,
)
from snowline_governance.scope_client import (
    HttpScopeClient,
    ScopeClient,
    ScopeNotFoundError,
    ScopeServiceError,
)
from snowline_governance.shadow import (
    BranchArchivedError,
    BranchNotFoundError,
    CONVERSATION_MESSAGE_KIND,
    MessageValidationError,
)

log = logging.getLogger("snowline.governance.turns")


class TurnError(RuntimeError):
    """A codex turn failed — nonzero exit, empty output, or a hard timeout. The
    loop turns this into an `agent.error` event so the failure is VISIBLE in the
    thread (fail-visible, spec §2)."""


# --- env helpers (loop-local, mirroring replication.py) --------------------

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _turns_enabled() -> bool:
    return os.environ.get("SNOWLINE_SHADOW_TURNS_ENABLED", "").strip().lower() in _TRUTHY


def _lenient_float(env_var: str, default: float) -> float:
    """A positive, finite float from `env_var`, else `default`. LENIENT (warn +
    fall back) like the SDK's heartbeat parser: a fat-fingered poll/timeout must
    not hot-loop or wedge the runner (`anyio.sleep(inf/nan)` never returns)."""
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("malformed %s=%r — using default %ss", env_var, raw, default)
        return default
    if not math.isfinite(value) or value <= 0:
        log.warning("out-of-range %s=%r — using default %ss", env_var, raw, default)
        return default
    return value


def _poll_seconds() -> float:
    return _lenient_float("SNOWLINE_SHADOW_TURN_POLL_SECONDS", 5.0)


def _turn_timeout() -> float:
    return _lenient_float("SNOWLINE_SHADOW_TURN_TIMEOUT", 300.0)


def _batch_limit() -> int:
    raw = os.environ.get("SNOWLINE_SHADOW_TURN_BATCH")
    if raw is None:
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning("malformed SNOWLINE_SHADOW_TURN_BATCH=%r — using 1", raw)
        return 1


def _turn_model() -> str | None:
    model = os.environ.get("SNOWLINE_SHADOW_TURN_MODEL", "").strip()
    return model or None


def _codex_bin() -> str:
    return os.environ.get("SNOWLINE_SHADOW_CODEX_BIN", "codex").strip() or "codex"


# --- pending-turn detection ------------------------------------------------


class PendingBranch(NamedTuple):
    """A branch awaiting an agent reply — its last conversation event is a human
    `message` (spec §6). `scope_slug` + `name` address it for context assembly."""

    branch_id: uuid.UUID
    scope_slug: str
    name: str
    # The human message's seq — the answered-ness fingerprint: if the branch's
    # max seq moved past this while the (long) turn ran, someone else already
    # answered or the human said more, and this turn's stale reply is dropped.
    last_seq: int


def find_pending_branches(session: Session) -> list[PendingBranch]:
    """Every ACTIVE branch whose LAST conversation event is a human `message` —
    the pending turns (spec §6). ONE query (Postgres `DISTINCT ON`): the
    highest-`seq` event per active branch, then Python-filters to human messages.

    ANSWERED SEMANTICS (a documented choice the spec left open, §6): a branch is
    pending ONLY when its last event is a human `message`. An `agent.error` (a
    failed turn) or an agent `message` (a delivered reply) as the last event
    means the turn is ANSWERED — the human must post a fresh message to retry.
    So a failed turn does not auto-retry on the next tick; it is terminal until
    the human follows up. Prevents a persistently-failing turn from hot-looping.
    """
    stmt = (
        select(
            ShadowBranch.id,
            ShadowBranch.scope_slug,
            ShadowBranch.name,
            ShadowConversationEvent.kind,
            ShadowConversationEvent.payload,
            ShadowConversationEvent.seq,
        )
        .join(
            ShadowConversationEvent,
            ShadowConversationEvent.branch_id == ShadowBranch.id,
        )
        .where(ShadowBranch.status == DEFAULT_SHADOW_BRANCH_STATUS)
        # DISTINCT ON (branch_id) + ORDER BY branch_id, seq DESC → the last event
        # per branch, one row each (no N+1 over branches).
        .distinct(ShadowBranch.id)
        .order_by(ShadowBranch.id, ShadowConversationEvent.seq.desc())
    )
    pending: list[PendingBranch] = []
    for bid, slug, name, kind, payload, seq in session.execute(stmt):
        if kind == CONVERSATION_MESSAGE_KIND and (payload or {}).get("author") == "human":
            pending.append(PendingBranch(bid, slug, name, seq))
    return pending


# --- claims (in-process, with a stale-claim reclaim) -----------------------


def _claimable(
    claims: dict[uuid.UUID, float], branch_id: uuid.UUID, now: float, timeout: float
) -> bool:
    """Is `branch_id` free to claim? Free if unclaimed, OR the existing claim is
    STALE — older than `timeout`. HONEST SCOPE: claims are in-process and
    per-tick (set and released within one sequential tick), so today this
    guards only against a future overlap of ticks/turns — NOT crash recovery
    (a process crash clears the dict with the process; the DB's answered-ness
    semantics are what make a re-run safe). `now`/claim times are
    `time.monotonic()` (immune to wall-clock jumps)."""
    claimed_at = claims.get(branch_id)
    if claimed_at is None:
        return True
    return (now - claimed_at) >= timeout


# --- context assembly ------------------------------------------------------

# Total prompt budget (spec §6). Over budget, sections drop in this DOCUMENTED
# order: OLDEST conversation turns first, then citations, then node rationales,
# then (backstop) the applicable-decisions grounding. The latest human message
# ALWAYS survives (a final head-truncate preserves it) — it's the turn's
# irreducible substance; node statements and narrative notes survive too except
# under the last-resort truncate.
PROMPT_CHAR_BUDGET = 24_000


@dataclass
class TurnContext:
    """The assembled, mutable context for one turn — mutable so the budget clamp
    can drop sections in place (spec §6)."""

    scope_slug: str
    branch_name: str
    narrative_notes: str | None
    nodes: list[dict] = field(default_factory=list)  # {statement, rationale, cites}
    history: list[dict] = field(default_factory=list)  # prior turns, oldest-first
    decisions: list[dict] = field(default_factory=list)
    decisions_available: bool = True
    latest_human: str = ""


def assemble_context(
    session: Session, scope_client: ScopeClient, scope_slug: str, name: str
) -> TurnContext:
    """Gather a branch's grounding (spec §6): name/scope/narrative notes, its
    nodes (statement + rationale) with citations, the conversation tail (via the
    existing `get_branch` tail cap), and the scope's applicable decisions.

    Grounding is BEST-EFFORT: if the platform (scope service) is unreachable the
    applicable-decisions read is skipped and the prompt notes it — a down
    platform must not kill turns (spec §6)."""
    branch = shadow.get_branch(session, scope_slug, name)

    nodes: list[dict] = []
    for n in branch["nodes"]:
        cites: list[str] = []
        for c in shadow.list_citations(session, n["id"]):
            if c["cited_decision_id"]:
                cites.append(f"real decision {c['cited_decision_id']}")
            elif c["cited_node_id"]:
                cites.append(f"shadow node {c['cited_node_id']}")
        nodes.append(
            {
                "statement": n["statement"],
                "rationale": n["rationale"],
                "cites": cites,
            }
        )

    # The conversation tail is oldest-first; the LAST entry is the pending human
    # message (that's what made the branch pending). Split it off as the message
    # to answer; the rest is prior-turn history.
    conv = branch.get("conversation") or []
    latest_human = conv[-1]["markdown"] if conv else ""
    history = [
        {"author": e["author"], "markdown": e["markdown"]} for e in conv[:-1]
    ]

    decisions_rows: list[dict] = []
    decisions_available = True
    try:
        decisions_rows = decisions.applicable_decisions(
            session, scope_slug, scope_client
        )["decisions"]
    except (ScopeServiceError, ScopeNotFoundError) as exc:
        decisions_available = False
        log.warning(
            "shadow turn: applicable decisions unavailable for scope %s "
            "(proceeding without grounding): %s",
            scope_slug,
            exc,
        )

    return TurnContext(
        scope_slug=scope_slug,
        branch_name=name,
        narrative_notes=branch.get("narrative_notes"),
        nodes=nodes,
        history=history,
        decisions=decisions_rows,
        decisions_available=decisions_available,
        latest_human=latest_human,
    )


# --- the prompt ------------------------------------------------------------

# The SHADOW-posture preamble (spec §6). Module-level so it's reviewable in one
# place. Frames the agent as a speculative co-thinker that CANNOT touch the real
# graph (and structurally couldn't — the runner only appends events).
PROMPT_PREAMBLE = """\
You are a speculative co-thinker inside an isolated SHADOW branch of a design
decision graph. You explore rival directions freely: propose, refute, weigh
trade-offs, and cite real decision ids where they ground the argument. You
CANNOT touch the real decision graph — this is a shadow space, and your reply is
only a message in the branch's conversation log; crystallizing anything into a
real decision is a human's separate act. Reply in concise markdown."""


def render_prompt(ctx: TurnContext) -> str:
    """Render `ctx` to the full prompt string: preamble, then context sections
    (branch, speculative nodes, applicable decisions, conversation so far), then
    the latest human message to respond to."""
    parts: list[str] = [PROMPT_PREAMBLE, ""]

    parts.append("## Branch")
    parts.append(f"scope: {ctx.scope_slug}")
    parts.append(f"name: {ctx.branch_name}")
    if ctx.narrative_notes:
        parts.append("")
        parts.append("### Narrative notes")
        parts.append(ctx.narrative_notes)
    parts.append("")

    if ctx.nodes:
        parts.append("## Speculative nodes")
        for i, n in enumerate(ctx.nodes, 1):
            parts.append(f"{i}. {n['statement']}")
            if n.get("rationale"):
                parts.append(f"   rationale: {n['rationale']}")
            if n.get("cites"):
                parts.append(f"   cites: {', '.join(n['cites'])}")
        parts.append("")

    parts.append("## Applicable decisions (real graph — grounding)")
    if not ctx.decisions_available:
        parts.append(
            "(platform unreachable — proceeding WITHOUT decision grounding)"
        )
    elif not ctx.decisions:
        parts.append("(none apply at this scope)")
    else:
        for d in ctx.decisions:
            frm = f" [from {d['from_scope']}]" if d.get("from_scope") else ""
            parts.append(f"- [{d['id']}]{frm} {d['decision']}")
    parts.append("")

    if ctx.history:
        parts.append("## Conversation so far")
        for e in ctx.history:
            parts.append(f"**{e['author']}**: {e['markdown']}")
        parts.append("")

    parts.append("## Respond to this message")
    parts.append(f"**human**: {ctx.latest_human}")

    return "\n".join(parts)


def clamp_prompt(ctx: TurnContext, budget: int = PROMPT_CHAR_BUDGET) -> str:
    """Render `ctx` clamped to `budget` chars, dropping in this DOCUMENTED order:
    (1) OLDEST conversation turns, (2) citations, (3) node rationales, then as a
    backstop (4) the applicable-decisions grounding (best-effort, spec §6). If
    even the irreducible core (preamble + node statements + the latest human
    message) overflows, a last-resort head-truncate PRESERVES the trailing human
    message — it's the turn's substance and the last section, so a naive
    `text[:budget]` would drop the very question being answered."""
    if len(render_prompt(ctx)) <= budget:
        return render_prompt(ctx)

    # 1. drop oldest conversation turns (the latest human message is separate and
    #    never dropped).
    while ctx.history and len(render_prompt(ctx)) > budget:
        ctx.history.pop(0)
    if len(render_prompt(ctx)) <= budget:
        return render_prompt(ctx)

    # 2. drop citations.
    for n in ctx.nodes:
        n["cites"] = []
    if len(render_prompt(ctx)) <= budget:
        return render_prompt(ctx)

    # 3. drop node rationales.
    for n in ctx.nodes:
        n["rationale"] = None
    if len(render_prompt(ctx)) <= budget:
        return render_prompt(ctx)

    # 4. drop the applicable-decisions grounding (a scope with many/long
    #    decisions can exceed the budget on its own; grounding is best-effort).
    ctx.decisions = []
    text = render_prompt(ctx)
    if len(text) <= budget:
        return text

    # Last resort: keep the trailing human message (the irreducible substance),
    # truncate the head. A plain `text[:budget]` would slice the question off the
    # end — the opposite of what must survive.
    tail = f"\n\n## Respond to this message\n**human**: {ctx.latest_human}"
    head = budget - len(tail)
    if head > 0:
        return text[:head] + tail
    # The question ALONE exceeds the budget (a near-cap 64 KiB message vs the
    # ~24k default). Keep the question's HEAD — its opening frames the ask —
    # and mark the cut explicitly rather than silently slicing off the end.
    marker = "\n…[message truncated to fit the turn budget]"
    return tail[: budget - len(marker)] + marker


# --- the codex inference seam (the ONLY LLM call in this module) ------------


def _invoke_codex(
    prompt: str, *, binary: str, model: str | None, timeout: float
) -> str:
    """Run ONE codex turn as a subprocess and return the agent's final message.

    Shape (verified, codex-cli 0.139.0):
      codex exec --sandbox read-only --ephemeral --skip-git-repo-check
        --color never -o <tmpfile> [-m <model>] -
    with the PROMPT on stdin and the final message written to `<tmpfile>`.

    Raises `TurnError` on nonzero exit, an empty output file, or a hard timeout
    (the process group is SIGKILLed on timeout — `start_new_session=True` gives
    codex its own group so a child it spawned dies too). This runs on a worker
    thread (the loop calls it via `anyio.to_thread.run_sync`), so blocking here
    is fine."""
    fd, out_path = tempfile.mkstemp(prefix="snowline-turn-", suffix=".md")
    os.close(fd)
    scratch_dir = tempfile.mkdtemp(prefix="snowline-turn-")
    try:
        cmd = [
            binary,
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-o",
            out_path,
        ]
        if model:
            cmd += ["-m", model]
        # An EMPTY scratch cwd (-C): the turn is pure reasoning over the stdin
        # prompt — nothing in the repo/home directory is legitimately useful to
        # it, and `--sandbox read-only` restricts WRITES, not reads. This does
        # not contain a determined prompt-injection (see spec §6's posture),
        # but it removes the casually-in-reach local context.
        cmd += ["-C", scratch_dir]
        cmd.append("-")  # read the prompt from stdin

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # own process group → killable as a group
        )
        try:
            _, stderr = proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.communicate()
            raise TurnError(f"codex turn timed out after {timeout:.0f}s") from None

        if proc.returncode != 0:
            detail = (stderr or "").strip()[:500]
            raise TurnError(f"codex exited {proc.returncode}: {detail}")

        reply = Path(out_path).read_text(encoding="utf-8").strip()
        if not reply:
            raise TurnError("codex produced an empty reply")
        return reply
    finally:
        Path(out_path).unlink(missing_ok=True)
        shutil.rmtree(scratch_dir, ignore_errors=True)


# --- one turn --------------------------------------------------------------


def process_turn(
    scope_client: ScopeClient,
    *,
    branch_id: uuid.UUID,
    scope_slug: str,
    name: str,
    last_seq: int,
    binary: str,
    model: str | None,
    timeout: float,
) -> None:
    """Run one turn end to end (spec §6): read context in one session, run codex
    with NO session held (the turn is LONG), then write the reply in a FRESH
    session. Success → an agent `message`; failure/timeout/empty → an
    `agent.error` (fail-visible). If the branch was archived mid-turn,
    `add_message` raises `BranchArchivedError` — we log and DROP the reply (we do
    NOT `append_error` to an archived branch, which also raises)."""
    # 1. Read context (one short session, released before the long codex call).
    with session_scope() as session:
        ctx = assemble_context(session, scope_client, scope_slug, name)
    prompt = clamp_prompt(ctx)

    # 2. Inference — NO DB session held across the subprocess.
    reply: str | None = None
    error: str | None = None
    try:
        reply = _invoke_codex(prompt, binary=binary, model=model, timeout=timeout)
    except TurnError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - any inference failure is fail-visible
        error = f"agent turn failed: {exc}"

    # 3. Write in a FRESH session (re-checking archived via the service's guard).
    try:
        with session_scope() as session:
            # Answered-ness re-check: the codex call is LONG (up to the turn
            # timeout). If the branch's log advanced past the human message we
            # answered (an agent MCP session replied, or the human said more),
            # this reply is stale — drop it; the next tick re-reads with the
            # fuller context. Shrinks the duplicate-reply window from the full
            # turn duration to microseconds (the residual race is accepted).
            current = session.scalar(
                select(func.max(ShadowConversationEvent.seq)).where(
                    ShadowConversationEvent.branch_id == branch_id
                )
            )
            if current != last_seq:
                log.info(
                    "shadow turn: branch %s advanced past seq %s while the "
                    "turn ran; dropping the stale reply",
                    branch_id,
                    last_seq,
                )
                return
            if reply is not None:
                try:
                    shadow.add_message(session, branch_id, reply, "agent")
                except MessageValidationError as exc:
                    # A DETERMINISTIC write rejection (the dominant case: a reply
                    # over the body cap). If this escaped, the branch would stay
                    # human-last → pending → re-run the ~300s codex EVERY poll,
                    # silently. Convert it to a fail-visible `agent.error` so the
                    # turn is ANSWERED (terminal) — the small error payload is
                    # well under the cap. `add_message` validates BEFORE any DB
                    # write, so the session is clean for this append.
                    shadow.append_error(
                        session, branch_id, f"agent reply rejected: {exc}"
                    )
            else:
                shadow.append_error(session, branch_id, error or "agent turn failed")
    except BranchArchivedError:
        # The branch was archived while the turn ran. An archived branch takes no
        # further events (append_error would raise too), so just drop the reply.
        log.info(
            "shadow turn: branch %s archived mid-turn; dropping reply", branch_id
        )
    except BranchNotFoundError:
        # The branch was deleted mid-turn (cascade). Nothing to write to.
        log.info(
            "shadow turn: branch %s gone mid-turn; dropping reply", branch_id
        )


# --- the loop --------------------------------------------------------------


def _tick(
    scope_client: ScopeClient,
    claims: dict[uuid.UUID, float],
    *,
    batch_limit: int,
    timeout: float,
    binary: str,
    model: str | None,
) -> None:
    """One poll tick: find pending branches, claim up to `batch_limit`
    unclaimed/stale ones, and process each. Processing is SEQUENTIAL within the
    tick (the loop runs one tick at a time off a single worker thread), so
    effective batch limit is capped by the loop shape — `batch_limit` only bounds
    how many pending branches a single tick drains. Each claim is released in a
    `finally`; the stale-claim reclaim (`_claimable`) is the backstop for a hard
    crash that skips the `finally`."""
    now = time.monotonic()
    with session_scope() as session:
        pending = find_pending_branches(session)

    processed = 0
    for pb in pending:
        if processed >= batch_limit:
            break
        if not _claimable(claims, pb.branch_id, now, timeout):
            continue
        claims[pb.branch_id] = now
        try:
            process_turn(
                scope_client,
                branch_id=pb.branch_id,
                scope_slug=pb.scope_slug,
                name=pb.name,
                last_seq=pb.last_seq,
                binary=binary,
                model=model,
                timeout=timeout,
            )
        except Exception:
            log.exception("shadow turn failed for branch %s; continuing", pb.branch_id)
        finally:
            claims.pop(pb.branch_id, None)
        processed += 1


async def shadow_turn_loop(scope_client: ScopeClient | None = None) -> None:
    """The turn-runner loop — rides the app lifespan task group beside
    `webhook_delivery_loop` (app.py). OFF by default: returns immediately unless
    `SNOWLINE_SHADOW_TURNS_ENABLED` is truthy (so tests and unconfigured deploys
    never run it).

    Each tick resolves the codex binary via `shutil.which` (a missing binary logs
    ONE warning and idles — the loop keeps checking each poll, so enabling the
    flag before installing codex can't crash-loop). One bad tick is swallowed +
    logged so the loop survives (mirroring the webhook / heartbeat loops). The
    blocking tick work runs off the event loop via `anyio.to_thread.run_sync`."""
    if not _turns_enabled():
        log.info("shadow turns disabled (SNOWLINE_SHADOW_TURNS_ENABLED unset/false)")
        return
    if scope_client is None:
        scope_client = HttpScopeClient()

    claims: dict[uuid.UUID, float] = {}
    warned_missing = False
    log.info("shadow turn-runner started (poll=%ss)", _poll_seconds())
    while True:
        try:
            binary = shutil.which(_codex_bin())
            if binary is None:
                if not warned_missing:
                    log.warning(
                        "codex binary %r not found on PATH; shadow turn loop idling "
                        "(will re-check each poll)",
                        _codex_bin(),
                    )
                    warned_missing = True
            else:
                warned_missing = False
                # abandon_on_cancel: lifespan shutdown must not wait out an
                # in-flight codex turn (up to the turn timeout — minutes). The
                # abandoned thread finishes in the background; its write either
                # lands before process exit or is lost, which the answered-ness
                # semantics tolerate (the human message stays last → the next
                # boot's tick re-runs the turn).
                await anyio.to_thread.run_sync(
                    functools.partial(
                        _tick,
                        scope_client,
                        claims,
                        batch_limit=_batch_limit(),
                        timeout=_turn_timeout(),
                        binary=binary,
                        model=_turn_model(),
                    ),
                    abandon_on_cancel=True,
                )
        except Exception:
            log.exception("shadow turn tick failed; loop continues")
        await anyio.sleep(_poll_seconds())
