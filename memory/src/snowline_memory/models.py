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

from sqlalchemy import Computed, String, Text, func
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
    """One working-memory note. `name` is the stable, kebab-case upsert key
    (UNIQUE); `remember` writes the same name to update in place. `description` is
    the one-line hook shown in the digest; `content` is the markdown body. `kind`
    is a soft enum (user/feedback/project/reference/gotcha). `scope_slug` is an
    optional soft scope reference (NULL ⇒ portfolio-wide)."""

    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="project")
    # Soft, optional scope reference — stored verbatim, never resolved to a
    # platform id, never a FK (scopes are platform-owned, in another DB).
    scope_slug: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    # Read-only mirror of the DB-generated FTS column (recall matches/ranks it).
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(TSVECTOR_EXPR, persisted=True), nullable=True
    )
