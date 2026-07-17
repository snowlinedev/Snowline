"""The memory substrate — remember / recall / digest / list / forget over
`Memory` rows.

Memory is FLAT and scope-tagged (no supersession, no inheritance walk;
memory-plugin spec §3). Its write model is a **per-name last-writer-wins
register with tombstoned deletes** (replication-continuity §4 coverage note,
#80) — the shape that makes it replication-safe under the checklist:

  - `remember` — LWW-upsert of the register named `name`: an incoming write wins
    iff its `(event_at, source_id)` strictly beats the stored clock (`_wins`,
    §6). In the ordinary single-instance case a local write's `now()` always
    wins, so this reads exactly like the old upsert-in-place; the clock only
    changes the outcome against a concurrent replicated write. Emits `memory.set`.
  - `forget` — TOMBSTONES the memory (`forgotten=True`) rather than hard-deleting,
    so a late-arriving OLDER `set` loses the LWW compare and cannot resurrect it.
    Emits `memory.forgotten`. Idempotent — a miss (or an already-tombstoned row)
    is a no-op that authors no event.
  - `apply_event` — the SDK ingest apply seam: a replicated `memory.set` /
    `memory.forgotten` converges the SAME register by the SAME `_wins` rule, so
    both instances reach the same state regardless of delivery order.
  - `recall` / `memory_digest` / `list_memories` — reads, all excluding
    tombstones. `recall` is FTS-ranked with a query (Postgres
    `websearch_to_tsquery` + `ts_rank` over the generated `search_vector`
    column; a multi-word query that strictly matches nothing relaxes to
    OR'd terms, #133), newest-first otherwise; filtered by kind + scope
    (scope ⊇ portfolio-wide).

`id`/`created_at`/`updated_at` are LOCAL bookkeeping and are NOT the converged
value — the register value is (content, description, kind, scope_slug, forgotten)
resolved by (`last_event_at`, `last_source_id`). Scope is a SOFT reference: a
slug is validated against the platform grammar (carried, not imported —
import-purity) and stored verbatim; it is never resolved against the platform.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from snowline_plugin_sdk.contract import EVENT_MEMORY_FORGOTTEN, EVENT_MEMORY_SET
from snowline_plugin_sdk.replication import emit_event
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from snowline_memory import config
from snowline_memory.models import Memory

# --- soft enum + defaults ---------------------------------------------------

# The soft-enum kinds (spec §4). "Soft" = an unknown value is stored verbatim,
# not rejected; this tuple only drives the default + the digest grouping order.
KINDS: tuple[str, ...] = ("user", "feedback", "project", "reference", "gotcha")
DEFAULT_KIND = "project"

DEFAULT_RECALL_LIMIT = 10
DEFAULT_LIST_LIMIT = 50
MAX_LIMIT = 200

# The FTS text-search configuration (matches governance's shadow corpus search).
_TS_CONFIG = "english"

# --- name / scope grammar ---------------------------------------------------

# Kebab-case: lowercase alphanumeric words joined by single hyphens (spec §4).
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Any run of characters that ISN'T lowercase-alnum collapses to a single hyphen
# when auto-normalizing a caller-provided name (underscores, spaces, punctuation,
# repeated hyphens — all become one separator).
_NAME_INVALID_RUN_RE = re.compile(r"[^a-z0-9]+")

# The platform scope slug grammar, CARRIED (not imported — import-purity) from
# `snowline_platform.scopes` §2.1: a bare org segment or `<org>/<rest>...`.
_SLUG_SEG = r"[._-]*[a-z0-9][a-z0-9._-]*"
SLUG_RE = re.compile(rf"^{_SLUG_SEG}(/{_SLUG_SEG})*$")

_DESCRIPTION_MAX = 200
_NAME_MAX = 80


class InvalidNameError(ValueError):
    """A supplied memory name is not valid kebab-case."""


class InvalidScopeError(ValueError):
    """A supplied scope slug doesn't match the platform slug grammar."""


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise InvalidNameError(
            f"invalid memory name {name!r} — must be kebab-case "
            "(lowercase alphanumerics joined by single hyphens)"
        )
    return name


def validate_scope(scope: str | None) -> str | None:
    """Validate an OPTIONAL scope slug against the platform grammar; return it
    verbatim (memory never resolves a slug — it's a soft reference). None passes
    through (portfolio-wide)."""
    if scope is None:
        return None
    scope = scope.strip()
    if not scope:
        return None
    if not SLUG_RE.match(scope):
        raise InvalidScopeError(
            f"invalid scope slug {scope!r} — expected a platform slug "
            "(`org` or `org/rest`)"
        )
    return scope


def normalize_name(name: str) -> str:
    """Auto-kebab-case a CALLER-PROVIDED name (issue #48): lowercase, collapse any
    run of non-`[a-z0-9]` characters (underscores, spaces, punctuation, repeated
    hyphens) to a single hyphen, strip leading/trailing hyphens, and clamp to
    `_NAME_MAX`. Unlike `slugify_to_name` (used only to GENERATE a name when one
    is omitted), this does NOT clamp to a handful of words — a caller's full
    intended name is preserved, just kebab-ified, so `remember`/`forget` don't
    silently truncate an explicit name. Applied at every name-accepting boundary
    so the same input always resolves to the same stored key (`my_note` and
    `my-note` collide). May return `""` for an all-punctuation input — callers
    must check for that."""
    slug = _NAME_INVALID_RUN_RE.sub("-", (name or "").strip().lower()).strip("-")
    return slug[:_NAME_MAX].strip("-")


def normalize_name_or_raise(name: str) -> str:
    """The single chokepoint for resolving a caller-PROVIDED name to its stored
    key: `normalize_name`, raising `InvalidNameError` when nothing survives (an
    all-punctuation name). `remember` and the importer's parse phase both call
    this — one source for the rule AND the message, so the importer's dry-run
    prediction can't drift from the live outcome."""
    normalized = normalize_name(name)
    if not normalized:
        raise InvalidNameError(
            f"invalid memory name {name!r} — name must be kebab-case "
            "(lowercase alphanumerics + hyphens)"
        )
    return normalized


def slugify_to_name(text: str) -> str:
    """Derive a kebab-case name from free text (the description or content head)
    when a caller omits `name`: `normalize_name`'s kebab transform on the first
    line, clamped to a handful of words so the generated key stays readable."""
    head = (text or "").strip().split("\n", 1)[0]
    # Keep it to the first ~8 words — a stable, readable key.
    slug = "-".join(normalize_name(head).split("-")[:8])
    return slug or "memory"


def _derive_description(content: str) -> str:
    """The one-line hook — the content's first non-empty line, clamped."""
    for line in (content or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= _DESCRIPTION_MAX else (
                line[: _DESCRIPTION_MAX - 1].rstrip() + "…"
            )
    return "(no description)"


def _row(m: Memory) -> dict:
    """The full read-shape for one memory row."""
    return {
        "id": str(m.id),
        "name": m.name,
        "description": m.description,
        "content": m.content,
        "kind": m.kind,
        "scope": m.scope_slug,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


def _header(m: Memory) -> dict:
    """The header (no content body) read-shape — for list/recall-newest views."""
    return {
        "name": m.name,
        "description": m.description,
        "kind": m.kind,
        "scope": m.scope_slug,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


def _resolve_limit(limit: int | None, default: int) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), MAX_LIMIT))


def _scope_filter(scope: str | None):
    """The scope-narrowing predicate: with a scope, that scope's rows PLUS
    portfolio-wide (`scope_slug IS NULL`) rows; without, no narrowing (all)."""
    if scope is None:
        return None
    return or_(Memory.scope_slug == scope, Memory.scope_slug.is_(None))


# --- LWW register core (#80) ------------------------------------------------


def _utcnow() -> datetime:
    """Naive UTC — the authoring clock stamped into local writes. Matches the
    SDK/monolith convention so `last_event_at` comparisons stay dialect-agnostic
    (naive both sides of the compare)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _monotonic_local_at(
    session: Session, name: str, source_id: str
) -> datetime:
    """The authoring clock for a LOCAL write: `now()`, but forced strictly greater
    than this instance's OWN last write to `name`. Without this, two rapid
    same-name writes (a `remember` then an immediate `forget`, say) could tie on
    the microsecond and the second would lose the strict `_wins` compare and be
    silently dropped. It bumps ONLY against our own prior write (same
    `source_id`) — a genuinely newer REMOTE state (a different source, e.g. a
    future-dated peer write under clock skew) still wins by strict LWW, so
    cross-instance convergence is untouched."""
    at = _utcnow()
    existing = session.scalar(select(Memory).where(Memory.name == name))
    if (
        existing is not None
        and existing.last_source_id == source_id
        and existing.last_event_at >= at
    ):
        at = existing.last_event_at + timedelta(microseconds=1)
    return at


def _event_at(payload: dict) -> datetime:
    """Parse a replicated event's authoring timestamp back to naive UTC. A
    well-formed emitter always stamps `event_at`; a missing one can't be ordered,
    so it sorts to `datetime.min` and loses the LWW compare against any real
    write (defensive, never expected)."""
    raw = payload.get("event_at")
    return datetime.fromisoformat(raw) if raw else datetime.min


def _wins(
    event_at: datetime,
    source_id: str,
    stored_at: datetime,
    stored_source_id: str,
) -> bool:
    """The §6 deterministic LWW merge — computed IDENTICALLY on every instance so
    a register converges without a resolution event: the incoming write wins iff
    `(event_at, source_id)` is strictly greater than the stored clock,
    lexicographically (timestamp first; `source_id` the stable tiebreak for a
    same-instant race). Strict `>` makes an exact re-delivery a no-op — the
    semantic idempotence §4 checklist item 4 requires."""
    return (event_at, source_id) > (stored_at, stored_source_id)


def _apply_set(
    session: Session,
    *,
    name: str,
    content: str,
    description: str,
    kind: str,
    scope_slug: str | None,
    event_at: datetime,
    source_id: str,
) -> tuple[Memory, bool]:
    """LWW-apply a `set` to the register named `name` — the shared core of the
    local `remember` and a replicated `memory.set`. Inserts when the name is
    absent; on an existing row it overwrites the value columns, CLEARS the
    tombstone, and advances the LWW clock ONLY when the incoming write strictly
    beats the stored clock (`_wins`) — otherwise it is a no-op (the stored write
    is newer). A winning set over a tombstone RESURRECTS the memory (the
    "a newer set beats the tombstone" criterion). Returns `(row, created)` where
    `created` is True only for a fresh insert."""
    existing = session.scalar(select(Memory).where(Memory.name == name))
    if existing is None:
        m = Memory(
            name=name,
            description=description,
            content=content,
            kind=kind,
            scope_slug=scope_slug,
            forgotten=False,
            last_event_at=event_at,
            last_source_id=source_id,
        )
        session.add(m)
        session.flush()
        session.refresh(m)
        return m, True
    if _wins(event_at, source_id, existing.last_event_at, existing.last_source_id):
        existing.content = content
        existing.description = description
        existing.kind = kind
        existing.scope_slug = scope_slug
        existing.forgotten = False
        existing.last_event_at = event_at
        existing.last_source_id = source_id
        existing.updated_at = func.now()
        session.flush()
        session.refresh(existing)
    return existing, False


def _apply_forget(
    session: Session,
    *,
    name: str,
    event_at: datetime,
    source_id: str,
) -> bool:
    """LWW-apply a tombstone to the register named `name`. Returns whether a LIVE
    row was tombstoned by this call.

    Unlike the local `forget` (which no-ops on a missing row — it authored no
    event), a REPLICATED forget CREATES a tombstone when the row is absent: the
    `set` it supersedes may still arrive later on a DIFFERENT stream (streams are
    only ordered internally), and the tombstone must already be present to WIN
    the LWW against that older set — otherwise the two instances diverge. On an
    existing row the tombstone advances only when it strictly beats the stored
    clock. Idempotent — re-delivery ties the clock and no-ops."""
    existing = session.scalar(select(Memory).where(Memory.name == name))
    if existing is None:
        session.add(
            Memory(
                name=name,
                description="",
                content="",
                kind=DEFAULT_KIND,
                scope_slug=None,
                forgotten=True,
                last_event_at=event_at,
                last_source_id=source_id,
            )
        )
        session.flush()
        return False
    if not _wins(
        event_at, source_id, existing.last_event_at, existing.last_source_id
    ):
        return False
    was_live = not existing.forgotten
    existing.forgotten = True
    existing.last_event_at = event_at
    existing.last_source_id = source_id
    existing.updated_at = func.now()
    session.flush()
    return was_live


# --- verbs ------------------------------------------------------------------


def remember(
    session: Session,
    content: str,
    name: str | None = None,
    description: str | None = None,
    kind: str | None = None,
    scope: str | None = None,
) -> dict:
    """Save durable working context, UPSERTING by `name`.

    A blank `content` is rejected. A PROVIDED `name` is auto-normalized to
    kebab-case (issue #48) — lowercased, with underscores/spaces/other invalid
    runs collapsed to single hyphens and clamped to `_NAME_MAX` — rather than
    hard-rejected, so `my_note` and `My Note Title` both save instead of erroring
    on the first attempt; it only raises if that normalization leaves nothing
    (an all-punctuation name). `name` is generated (kebab, from `slugify_to_name`)
    from the description/content head when omitted; `description` is derived from
    the content's first line when omitted; `kind` defaults to `project` and is
    LOWERCASED at this boundary (soft enum — `Gotcha` and `gotcha` are the same
    kind, so the digest never splits on case); `scope` is an optional, validated,
    verbatim-stored slug. If a memory with the NORMALIZED `name` already exists it
    is updated IN PLACE (content/description/kind/scope), bumping `updated_at` on
    EVERY upsert — including an identical-content re-remember (the write is a
    deliberate touch) — so `my_note` then `my-note` collide as the same memory.
    Returns the full row plus `created` (True on insert, False on in-place
    update)."""
    if not content or not content.strip():
        raise ValueError("`content` must be a non-empty string")
    content = content.strip()

    kind = (kind or DEFAULT_KIND).strip().lower() or DEFAULT_KIND
    scope = validate_scope(scope)
    description = (description or "").strip() or _derive_description(content)
    if len(description) > _DESCRIPTION_MAX:
        description = description[: _DESCRIPTION_MAX - 1].rstrip() + "…"

    # A whitespace-only name counts as omitted, same as None/"" — only a name
    # with real characters goes down the provided-name path.
    if name and name.strip():
        name = normalize_name_or_raise(name)
    else:
        name = validate_name(slugify_to_name(description or content))

    # The authoring clock for this write (#80): local `now()` (made strictly
    # monotonic against our own prior write to this name) almost always wins the
    # LWW compare, so the common single-instance path is still an in-place upsert
    # that bumps `updated_at`. The event carries the SAME clock, so a peer
    # computes the identical `_wins` verdict.
    src = config.source_id()
    event_at = _monotonic_local_at(session, name, src)
    m, created = _apply_set(
        session,
        name=name,
        content=content,
        description=description,
        kind=kind,
        scope_slug=scope,
        event_at=event_at,
        source_id=src,
    )
    # Emit the authored `set` regardless of the local verdict: a set that lost
    # locally (a newer replicated write already present) loses on every peer too,
    # a harmless no-op — so emission stays unconditional and the outbox seq
    # ordering never has to reason about win/lose.
    emit_event(
        session,
        EVENT_MEMORY_SET,
        {
            "id": str(m.id),
            "name": name,
            "description": description,
            "content": content,
            "kind": kind,
            "scope_slug": scope,
            "event_at": event_at.isoformat(),
            "source_id": src,
        },
    )
    row = _row(m)
    row["created"] = created
    return row


def _any_term_tsquery(query: str):
    """OR the query's terms into one tsquery — the relaxed fallback for when the
    strict `websearch_to_tsquery` (which ANDs every term, #133) matches nothing.
    Pure-operator tokens (`OR`/`AND`, `-negations`) are dropped rather than OR'd
    (a lone negation would match nearly everything). Returns None when nothing
    searchable survives."""
    words = [
        w
        for w in re.split(r"\s+", query)
        if w and w.upper() not in ("OR", "AND") and not w.startswith("-")
    ]
    if not words:
        return None
    tsq = func.websearch_to_tsquery(_TS_CONFIG, words[0])
    for w in words[1:]:
        tsq = tsq.op("||")(func.websearch_to_tsquery(_TS_CONFIG, w))
    return tsq


def recall(
    session: Session,
    query: str | None = None,
    kind: str | None = None,
    scope: str | None = None,
    limit: int | None = DEFAULT_RECALL_LIMIT,
) -> dict:
    """Search working memory. With `query`: FTS-ranked over name+description+content
    (Postgres `websearch_to_tsquery` + `ts_rank` on the generated `search_vector`
    column). The strict query ANDs every term; when a MULTI-word query matches
    nothing that way, recall retries with the terms OR'd (#133 — a natural query
    like "walkthrough plugin usage registration" shouldn't return 0 because one
    word misses) and `match_mode` reports "any_term" so the caller knows the
    results are relaxed matches, best-first by how many terms hit. Without a
    query: newest-first. `kind` filters exactly; `scope` returns that scope's
    rows PLUS portfolio-wide rows. Returns full rows + `items_total`."""
    scope = validate_scope(scope)
    lim = _resolve_limit(limit, DEFAULT_RECALL_LIMIT)

    # Tombstones (forgotten memories, #80) are never read.
    filters = [Memory.forgotten.is_(False)]
    if kind:
        filters.append(Memory.kind == kind.strip())
    sf = _scope_filter(scope)
    if sf is not None:
        filters.append(sf)

    query = (query or "").strip()
    match_mode = None
    if query:

        def _fts(tsq):
            match = Memory.search_vector.op("@@")(tsq)
            rank = func.ts_rank(Memory.search_vector, tsq)
            stmt = (
                select(Memory, rank.label("rank"))
                .where(match, *filters)
                .order_by(rank.desc(), Memory.updated_at.desc(), Memory.name.asc())
                .limit(lim)
            )
            rows = list(session.execute(stmt))
            total = session.scalar(
                select(func.count()).select_from(Memory).where(match, *filters)
            ) or 0
            return [{**_row(m), "rank": float(r)} for m, r in rows], total

        match_mode = "all_terms"
        items, total = _fts(func.websearch_to_tsquery(_TS_CONFIG, query))
        if total == 0 and len(query.split()) > 1:
            relaxed = _any_term_tsquery(query)
            if relaxed is not None:
                items, total = _fts(relaxed)
                if total:
                    match_mode = "any_term"
    else:
        stmt = (
            select(Memory)
            .where(*filters)
            .order_by(Memory.updated_at.desc(), Memory.name.asc())
            .limit(lim)
        )
        items = [_row(m) for m in session.scalars(stmt)]
        total = session.scalar(
            select(func.count()).select_from(Memory).where(*filters)
        ) or 0

    out = {
        "query": query or None,
        "kind": kind or None,
        "scope": scope,
        "memories": items,
        "items_total": total,
    }
    if match_mode is not None:
        out["match_mode"] = match_mode
    return out


def memory_digest(session: Session, scope: str | None = None) -> dict:
    """The session-start read: EVERY applicable memory as a one-line
    `name — description` entry, grouped by kind. Cheap and deterministic (no FTS,
    no ranking). With `scope`: that scope's rows PLUS portfolio-wide rows;
    without: all memories. Kinds appear in the soft-enum order first, then any
    novel kinds alphabetically; entries within a kind are name-sorted so the
    digest is stable across calls."""
    scope = validate_scope(scope)
    filters = [Memory.forgotten.is_(False)]  # tombstones are never read (#80)
    sf = _scope_filter(scope)
    if sf is not None:
        filters.append(sf)

    stmt = select(Memory).where(*filters).order_by(Memory.name.asc())
    rows = list(session.scalars(stmt))

    groups: dict[str, list[dict]] = {}
    for m in rows:
        groups.setdefault(m.kind, []).append(
            {"name": m.name, "description": m.description, "scope": m.scope_slug}
        )

    # Soft-enum kinds first (in declared order), then any novel kinds sorted.
    ordered_kinds = [k for k in KINDS if k in groups] + sorted(
        k for k in groups if k not in KINDS
    )
    grouped = [{"kind": k, "entries": groups[k]} for k in ordered_kinds]

    return {
        "scope": scope,
        "items_total": len(rows),
        "groups": grouped,
    }


def list_memories(
    session: Session,
    kind: str | None = None,
    scope: str | None = None,
    limit: int | None = DEFAULT_LIST_LIMIT,
) -> dict:
    """Browse memory headers (name, description, kind, scope, updated_at),
    newest-first, filtered like `recall` (kind exact; scope ⊇ portfolio-wide).
    The hygiene twin of a query-less `recall` — a lighter header shape."""
    scope = validate_scope(scope)
    lim = _resolve_limit(limit, DEFAULT_LIST_LIMIT)

    filters = [Memory.forgotten.is_(False)]  # tombstones are never read (#80)
    if kind:
        filters.append(Memory.kind == kind.strip())
    sf = _scope_filter(scope)
    if sf is not None:
        filters.append(sf)

    stmt = (
        select(Memory)
        .where(*filters)
        .order_by(Memory.updated_at.desc(), Memory.name.asc())
        .limit(lim)
    )
    items = [_header(m) for m in session.scalars(stmt)]
    total = session.scalar(
        select(func.count()).select_from(Memory).where(*filters)
    ) or 0
    return {
        "kind": kind or None,
        "scope": scope,
        "memories": items,
        "items_total": total,
    }


def forget(session: Session, name: str) -> dict:
    """TOMBSTONE one memory by name (#80) — mark it `forgotten` rather than
    hard-deleting, so a late-arriving OLDER `set` cannot resurrect it. `name` is
    normalized the same way `remember` normalizes a provided name (kebab-cased),
    so a caller who saved `my_note` (stored as `my-note`) can still
    `forget("my_note")`. Emits `memory.forgotten` on success.

    Idempotent — a miss (no such name) OR an already-tombstoned row reports
    `{forgotten: False}` and authors NO event: a forget that tombstones nothing
    live carries no information to converge, and not minting a tombstone for an
    unknown local name keeps the store from accreting phantom rows (a REPLICATED
    forget, by contrast, does create one — see `_apply_forget`)."""
    # Deliberately bare normalize_name (not normalize_name_or_raise): an
    # unnormalizable name can't match any stored key, and under the idempotent
    # contract "nothing to forget" is a miss, not an error.
    name = normalize_name(name)
    existing = session.scalar(select(Memory).where(Memory.name == name))
    if existing is None or existing.forgotten:
        return {"forgotten": False, "name": name}

    src = config.source_id()
    # Same monotonic-clock guard as `remember` (see `_monotonic_local_at`): a
    # forget immediately after a same-instance set must not tie the microsecond
    # and lose. Bump only against our own prior write; a newer remote write still
    # wins the compare below.
    event_at = _utcnow()
    if existing.last_source_id == src and existing.last_event_at >= event_at:
        event_at = existing.last_event_at + timedelta(microseconds=1)
    if not _wins(
        event_at, src, existing.last_event_at, existing.last_source_id
    ):
        # A newer write already beat this forget (a future-dated remote set on a
        # skewed clock) — the live row stands. Vanishingly rare under sane clocks
        # (§6); reported as a miss rather than claiming a delete that didn't land.
        return {"forgotten": False, "name": name}

    existing.forgotten = True
    existing.last_event_at = event_at
    existing.last_source_id = src
    existing.updated_at = func.now()
    session.flush()
    emit_event(
        session,
        EVENT_MEMORY_FORGOTTEN,
        {
            "id": str(existing.id),
            "name": name,
            "event_at": event_at.isoformat(),
            "source_id": src,
        },
    )
    return {"forgotten": True, "name": name}


# --- replication apply seam (#80) -------------------------------------------


def apply_event(session: Session, envelope: dict) -> None:
    """The idempotent domain apply the SDK ingest runs for a delivered event
    (replication-continuity §4 checklist item 4; wired via
    `build_replication_router` in `app.py`). `envelope` is the v2 stream envelope;
    the memory domain body is `envelope["payload"]`. Convergence is per-name LWW
    (§6): a `memory.set` LWW-upserts the register, a `memory.forgotten`
    LWW-tombstones it — both computed by the SAME `_wins` rule the local verbs
    use, so the two instances reach the same state regardless of delivery order.

    Runs UNDER the SDK's origin suppression, and this seam never calls
    `emit_event` itself, so an applied write can never re-emit (§3.2 hard rule).
    Raises on an unknown event type — every apply exception is §8.1-retryable, so
    a genuinely poison event parks loudly rather than being silently dropped."""
    event_type = envelope.get("event_type")
    payload = envelope.get("payload") or {}
    name = payload["name"]
    event_at = _event_at(payload)
    src = payload.get("source_id") or ""

    if event_type == EVENT_MEMORY_SET:
        _apply_set(
            session,
            name=name,
            content=payload.get("content", ""),
            description=payload.get("description", ""),
            kind=payload.get("kind", DEFAULT_KIND),
            scope_slug=payload.get("scope_slug"),
            event_at=event_at,
            source_id=src,
        )
    elif event_type == EVENT_MEMORY_FORGOTTEN:
        _apply_forget(session, name=name, event_at=event_at, source_id=src)
    else:
        raise ValueError(f"unknown memory event type {event_type!r}")
