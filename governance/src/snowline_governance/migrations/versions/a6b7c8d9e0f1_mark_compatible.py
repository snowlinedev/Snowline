"""decision_concurrences.marked_compatible_at (§6.1 explicit compatibility)

The missing half of replication-continuity §6.1's reconciliation sentence — the
flag clears once the supersession edge exists "(or the pair is explicitly marked
compatible)" (issue #97). A nullable timestamp on the existing
`decision_concurrences` marker row: NULL until a human judges the pair
compatible, non-NULL forever after (permanent, idempotent — decisions are
content-immutable, so the judgment can never go stale). The `unreconciled` view
derives `both members leaves AND marked_compatible_at IS NULL`.

Additive column on a table created in f5a6b7c8d9e0 — a NEW migration, never an
edit to the old one.

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-07-04

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a6b7c8d9e0f1"
down_revision: str | None = "f5a6b7c8d9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "decision_concurrences",
        sa.Column("marked_compatible_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("decision_concurrences", "marked_compatible_at")
