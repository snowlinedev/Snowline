"""persist §4 transition warnings on the transition log

QA feedback 6396993e: lifecycle responses now WARN on unmet-dependency
activation/achievement and cancel-from-active (PR #155), but the warning was
response-only — nothing durable or readable afterward, so an achieved
milestone could still silently contradict its dependency graph in every later
read. The warnings computed at transition time now persist on the transition
row itself (additive nullable JSON) and ride the transition read + the
replication payload.

Revision ID: a7b9c1d3e5f8
Revises: f6a8b0c2d4e6
Create Date: 2026-07-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b9c1d3e5f8"
down_revision: str | None = "f6a8b0c2d4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "milestone_transitions",
        sa.Column("warnings", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("milestone_transitions", "warnings")
