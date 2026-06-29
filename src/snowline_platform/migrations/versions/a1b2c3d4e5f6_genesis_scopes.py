"""genesis: scopes

The platform's first persisted table — the scope namespace (architecture.md §2,
scope-namespace spec §2). Columns/types are kept COMPATIBLE with the frozen
monolith's trimmed-core `scopes` table (monolith #650: identity + tree +
isolation) so existing scope rows import cleanly into a running platform instance
later (spec §6). PM/governance freight (external_key, code_sweep, roadmap,
spec_repo) lives in the plugins, not here.

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-06-29

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scopes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "isolated",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(["parent_id"], ["scopes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )


def downgrade() -> None:
    op.drop_table("scopes")
