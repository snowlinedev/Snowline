"""genesis: memories

Memory's first (and, this increment, only) persisted table — the flat,
scope-tagged, upsert-in-place working-memory store (memory-plugin spec §4).

`name` is UNIQUE (the upsert key). `scope_slug` is a SOFT reference (no
cross-service FK — scopes are platform-owned, in another DB); NULL ⇒
portfolio-wide. Full-text search is a Postgres-native GENERATED `tsvector` column
(`search_vector`, `GENERATED ALWAYS AS to_tsvector(...) STORED`) with a GIN index
— the monolith's stored-column pattern. The generated expression is kept in ONE
place (`models.TSVECTOR_EXPR`) so the ORM's `Computed(...)` and this DDL can't
drift.

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-07-02

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

from snowline_memory.models import TSVECTOR_EXPR


revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        # Soft scope reference (platform-owned scope; no cross-service FK).
        sa.Column("scope_slug", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Postgres-native generated FTS column — the stored-column pattern.
        sa.Column(
            "search_vector",
            TSVECTOR(),
            sa.Computed(TSVECTOR_EXPR, persisted=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_memories_name"),
    )
    # Scope filtering (recall/digest/list narrow on the soft scope reference).
    op.create_index(
        "ix_memories_scope_slug", "memories", ["scope_slug"], unique=False
    )
    # The FTS match/rank query uses this GIN index over the generated tsvector.
    op.create_index(
        "ix_memories_search_vector",
        "memories",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_memories_search_vector", table_name="memories")
    op.drop_index("ix_memories_scope_slug", table_name="memories")
    op.drop_table("memories")
