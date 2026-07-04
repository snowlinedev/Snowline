"""Memory's persisted model — a single `Memory` table.

Memory is a FLAT, scope-tagged, upsert-in-place store of working context (no
supersession graph, no inheritance walk, no events — those are governance's;
memory-plugin spec §3 / §8). One `Base` / one metadata; its alembic chain targets
THIS metadata (separate from the platform's and governance's).

Scope is a SOFT reference: `scope_slug` is stored verbatim (validated against the
platform slug grammar when present), NOT a `ForeignKey` — scopes are
platform-owned and live in another database. NULL ⇒ portfolio-wide.

Full-text search is a Postgres-native GENERATED `tsvector` column (`search_vector`,
`GENERATED ALWAYS AS to_tsvector(...) STORED`) with a GIN index — the monolith's
stored-column pattern (spec §4). SQLAlchemy renders the generated column via
`Computed(...)`; the actual DDL (column + GIN index) lives in the genesis
migration, the single source of truth the app boot-migrates to.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Computed, DateTime, Index, String, Text, false, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# The generated tsvector expression — kept in ONE place and referenced by both the
# model's Computed() and the genesis migration, so the ORM view and the DDL can't
# drift. `coalesce` guards NULLs (description/content are NOT NULL, but the
# expression stays robust if that ever loosens).
TSVECTOR_EXPR = (
    "to_tsvector('english', "
    "coalesce(name, '') || ' ' || "
    "coalesce(description, '') || ' ' || "
    "coalesce(content, ''))"
)


class Memory(Base):
    """One working-memory note. `name` is the stable, kebab-case key — and, since
    the replication rework (replication-continuity §4 note, #80), the LOGICAL
    identity of the last-writer-wins register a memory named X *is*: `remember`
    and a replicated `memory.set` both converge the row named X by LWW, and
    `forget` TOMBSTONES it (`forgotten=True`) rather than hard-deleting, so a
    late-arriving OLDER `set` cannot resurrect it. `id`/`created_at`/`updated_at`
    are LOCAL bookkeeping (a surrogate key + local write times) and are NOT the
    converged value — the register value is
    (content, description, kind, scope_slug, forgotten) resolved by
    (`last_event_at`, `last_source_id`). `description` is the one-line hook shown
    in the digest; `content` is the markdown body. `kind` is a soft enum
    (user/feedback/project/reference/gotcha). `scope_slug` is an optional soft
    scope reference (NULL ⇒ portfolio-wide)."""

    __tablename__ = "memories"
    __table_args__ = (
        # Declared to MATCH the genesis migration's indexes exactly (names and
        # all), so an autogenerate diff can't propose dropping them — the model
        # and the DDL stay in parity.
        Index("ix_memories_scope_slug", "scope_slug"),
        Index("ix_memories_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="project")
    # Soft, optional scope reference — stored verbatim, never resolved to a
    # platform id, never a FK (scopes are platform-owned, in another DB).
    scope_slug: Mapped[str | None] = mapped_column(String, nullable=True)
    # The TOMBSTONE flag (#80): a forgotten memory is kept as a tombstone (never
    # hard-deleted) so a late-arriving OLDER `set` loses the LWW compare and
    # cannot resurrect it. Excluded from every read (recall/digest/list).
    forgotten: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # The LWW clock for the register named `name` (replication-continuity §6):
    # the AUTHORING timestamp of the last winning write (local now() or a
    # replicated event's `event_at`), with `last_source_id` (`<instance>.memory`)
    # as the stable tiebreak. An incoming write wins iff
    # (event_at, source_id) > (last_event_at, last_source_id) — computed
    # identically on every instance, so the register converges. Distinct from
    # `updated_at`, which is server-time and bumps on any touch.
    last_event_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_source_id: Mapped[str] = mapped_column(
        String, nullable=False, server_default="legacy"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    # Read-only mirror of the DB-generated FTS column (recall matches/ranks it).
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(TSVECTOR_EXPR, persisted=True), nullable=True
    )
