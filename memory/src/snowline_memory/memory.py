"""The memory substrate — remember / recall / digest / list / forget over
`Memory` rows.

Memory is FLAT and scope-tagged (no supersession, no inheritance, no events;
memory-plugin spec §3). The behaviors here are:

  - `remember` — upsert by kebab-case `name` (same name updates in place); name
    and description are generated/derived when omitted.
  - `recall` — FTS-ranked when a query is given (Postgres `websearch_to_tsquery`
    + `ts_rank` over the generated `search_vector` column), newest-first
    otherwise; filtered by kind + scope (scope ⊇ portfolio-wide).
  - `memory_digest` — the cheap, deterministic session-start read: every
    applicable memory as a one-line `name — description` entry, grouped by kind.
  - `list_memories` — the hygiene/browse twin of a query-less recall.
  - `forget` — delete one memory by name (idempotent).

Scope is a SOFT reference: a slug is validated against the platform grammar
(carried, not imported — import-purity) and stored verbatim; it is never resolved
against the platform.
"""

from __future__ import annotations

import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

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


def slugify_to_name(text: str) -> str:
    """Derive a kebab-case name from free text (the description or content head)
    when a caller omits `name`. Lowercase, non-alphanumeric runs → single hyphens,
    trimmed, clamped to a handful of words so the generated key stays readable."""
    head = (text or "").strip().split("\n", 1)[0]
    slug = re.sub(r"[^a-z0-9]+", "-", head.lower()).strip("-")
    # Keep it to the first ~8 words / _NAME_MAX chars — a stable, readable key.
    slug = "-".join(slug.split("-")[:8])[:_NAME_MAX].strip("-")
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

    A blank `content` is rejected. `name` (kebab) is generated from the
    description/content head when omitted; `description` is derived from the
    content's first line when omitted; `kind` defaults to `project`; `scope` is an
    optional, validated, verbatim-stored slug. If a memory with `name` already
    exists it is updated IN PLACE (content/description/kind/scope, bumping
    `updated_at`); otherwise a new row is inserted. Returns the full row plus
    `created` (True on insert, False on in-place update)."""
    if not content or not content.strip():
        raise ValueError("`content` must be a non-empty string")
    content = content.strip()

    kind = (kind or DEFAULT_KIND).strip() or DEFAULT_KIND
    scope = validate_scope(scope)
    description = (description or "").strip() or _derive_description(content)
    if len(description) > _DESCRIPTION_MAX:
        description = description[: _DESCRIPTION_MAX - 1].rstrip() + "…"

    if name:
        name = validate_name(name)
    else:
        name = validate_name(slugify_to_name(description or content))

    existing = session.scalar(select(Memory).where(Memory.name == name))
    if existing is not None:
        existing.content = content
        existing.description = description
        existing.kind = kind
        existing.scope_slug = scope
        session.flush()
        session.refresh(existing)
        row = _row(existing)
        row["created"] = False
        return row

    m = Memory(
        name=name,
        description=description,
        content=content,
        kind=kind,
        scope_slug=scope,
    )
    session.add(m)
    session.flush()
    session.refresh(m)
    row = _row(m)
    row["created"] = True
    return row


def recall(
    session: Session,
    query: str | None = None,
    kind: str | None = None,
    scope: str | None = None,
    limit: int | None = DEFAULT_RECALL_LIMIT,
) -> dict:
    """Search working memory. With `query`: FTS-ranked over name+description+content
    (Postgres `websearch_to_tsquery` + `ts_rank` on the generated `search_vector`
    column). Without: newest-first. `kind` filters exactly; `scope` returns that
    scope's rows PLUS portfolio-wide rows. Returns full rows + `items_total`."""
    scope = validate_scope(scope)
    lim = _resolve_limit(limit, DEFAULT_RECALL_LIMIT)

    filters = []
    if kind:
        filters.append(Memory.kind == kind.strip())
    sf = _scope_filter(scope)
    if sf is not None:
        filters.append(sf)

    query = (query or "").strip()
    if query:
        tsq = func.websearch_to_tsquery(_TS_CONFIG, query)
        match = Memory.search_vector.op("@@")(tsq)
        rank = func.ts_rank(Memory.search_vector, tsq)
        stmt = (
            select(Memory, rank.label("rank"))
            .where(match, *filters)
            .order_by(rank.desc(), Memory.updated_at.desc(), Memory.name.asc())
            .limit(lim)
        )
        rows = list(session.execute(stmt))
        items = [{**_row(m), "rank": float(r)} for m, r in rows]
        total = session.scalar(
            select(func.count()).select_from(Memory).where(match, *filters)
        ) or 0
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

    return {
        "query": query or None,
        "kind": kind or None,
        "scope": scope,
        "memories": items,
        "items_total": total,
    }


def memory_digest(session: Session, scope: str | None = None) -> dict:
    """The session-start read: EVERY applicable memory as a one-line
    `name — description` entry, grouped by kind. Cheap and deterministic (no FTS,
    no ranking). With `scope`: that scope's rows PLUS portfolio-wide rows;
    without: all memories. Kinds appear in the soft-enum order first, then any
    novel kinds alphabetically; entries within a kind are name-sorted so the
    digest is stable across calls."""
    scope = validate_scope(scope)
    filters = []
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

    filters = []
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
    """Delete one memory by name. Idempotent — a miss reports
    `{forgotten: False}` rather than raising."""
    name = (name or "").strip()
    m = session.scalar(select(Memory).where(Memory.name == name))
    if m is None:
        return {"forgotten": False, "name": name}
    session.delete(m)
    session.flush()
    return {"forgotten": True, "name": name}
