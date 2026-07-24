"""milestone replication

The milestone registry adopts the replication-class contract (milestones.md §9,
issue #145), the same contract scopes dogfood — but milestones need the §6 LWW
rules stated, not implied (a lifecycle state machine plus a mutable DAG). This
migration carries the persistence that convergence needs:

  * `milestones.lww_authored_at` / `lww_source_id` — the ROW-LEVEL last-writer-wins
    clock: the authored-at + authoring source_id of the write that last set the
    row's mutable state. Apply converges by comparing an incoming event's
    `(authored_at, source_id)` against these (§6, source_id breaking a tie).
  * `milestone_transitions.source_id` — the authoring instance per transition:
    the LWW tiebreak, the redelivery dedupe key, and the deterministic secondary
    sort for the §4 illegal-history reconstruction. Both instances' transitions
    converge into this one log (the LWW loser retained).
  * `milestone_unreconciled` — first-class unreconciled state for agent triage:
    a converged transition log implying an illegal move (e.g. cancelled->active)
    is surfaced here, NOT parked (parking would block the convergence §6 requires).

All additive: the existing columns/tables are untouched, and the new columns are
nullable, so rows written before replication was configured need no backfill.

Revision ID: e5f7a9b1c3d5
Revises: d4e6f8a0b2c4
Create Date: 2026-07-23

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e5f7a9b1c3d5"
down_revision: str | None = "d4e6f8a0b2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "milestones",
        sa.Column("lww_authored_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "milestones",
        sa.Column("lww_source_id", sa.String(), nullable=True),
    )
    op.add_column(
        "milestone_transitions",
        sa.Column("source_id", sa.String(), nullable=True),
    )
    op.create_table(
        "milestone_unreconciled",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("milestone_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("milestone_unreconciled")
    op.drop_column("milestone_transitions", "source_id")
    op.drop_column("milestones", "lww_source_id")
    op.drop_column("milestones", "lww_authored_at")
