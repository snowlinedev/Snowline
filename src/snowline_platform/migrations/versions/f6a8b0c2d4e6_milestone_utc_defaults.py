"""milestone timestamps: one clock per row (naive UTC)

QA feedback 4c566e49: `created_at`/`updated_at` defaulted to plain `now()` —
the DB session's LOCAL clock — while the lifecycle stamps (`activated_at`,
transition `authored_at`, the LWW clock) are Python naive-UTC, so a single
milestone row mixed two clocks hours apart. The milestone family's server
defaults become `timezone('utc', now())` so raw-SQL inserts agree with the
ORM's Python-side `_utcnow` defaults (models.py). Existing rows are left
as-written — the skew is a display/audit artifact on a handful of drill/QA
rows, and rewriting history stamps is worse than annotating the change here.
Scope rows are untouched (server clock used consistently; normalization there
rides the #98 scope-payload work).

Revision ID: f6a8b0c2d4e6
Revises: e5f7a9b1c3d5
Create Date: 2026-07-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a8b0c2d4e6"
down_revision: str | None = "e5f7a9b1c3d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLS = (
    ("milestones", "created_at"),
    ("milestones", "updated_at"),
    ("milestone_transitions", "authored_at"),
    ("milestone_dependencies", "created_at"),
    ("milestone_unreconciled", "created_at"),
)


def upgrade() -> None:
    for table, col in _COLS:
        op.alter_column(
            table, col, server_default=sa.text("timezone('utc'::text, now())")
        )


def downgrade() -> None:
    for table, col in _COLS:
        op.alter_column(table, col, server_default=sa.text("now()"))
