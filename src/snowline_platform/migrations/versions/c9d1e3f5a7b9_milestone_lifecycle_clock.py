"""milestone lifecycle LWW clock (`lifecycle_authored_at` / `lifecycle_source_id`)

snowlinedev/Snowline#247: a concurrent `update` (outcome/target_date), applied as
a full-row last-writer-wins write, could silently revert a lifecycle transition
authored on another instance. The fix splits each milestone row into two LWW
registers (milestones.md §9): the DESCRIPTIVE register keeps the existing
`lww_authored_at` / `lww_source_id` clock, and the LIFECYCLE register (status +
the `*_at` stamps) gets its own clock, added here.

Backfill: the latest transition-log entry's `(authored_at, source_id)` when the
milestone has one (the converged lifecycle winner), else the row's existing
clock (a never-transitioned row's lifecycle was set at create). Additive and
nullable — a row with no clock at all stays NULL (sorts lowest on apply, as
before).

Revision ID: c9d1e3f5a7b9
Revises: b8c0d2e4f6a8
Create Date: 2026-10-09

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d1e3f5a7b9"
down_revision: str | None = "b8c0d2e4f6a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "milestones",
        sa.Column("lifecycle_authored_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "milestones",
        sa.Column("lifecycle_source_id", sa.String(), nullable=True),
    )
    op.execute(
        """
        UPDATE milestones m
           SET lifecycle_authored_at = COALESCE(t.authored_at, m.lww_authored_at),
               lifecycle_source_id = CASE WHEN t.authored_at IS NOT NULL
                                          THEN t.source_id
                                          ELSE m.lww_source_id END
          FROM milestones m2
          LEFT JOIN LATERAL (
                SELECT authored_at, source_id
                  FROM milestone_transitions
                 WHERE milestone_id = m2.id AND authored_at IS NOT NULL
              ORDER BY authored_at DESC, source_id DESC NULLS LAST
                 LIMIT 1
          ) t ON true
         WHERE m2.id = m.id
        """
    )


def downgrade() -> None:
    op.drop_column("milestones", "lifecycle_source_id")
    op.drop_column("milestones", "lifecycle_authored_at")
