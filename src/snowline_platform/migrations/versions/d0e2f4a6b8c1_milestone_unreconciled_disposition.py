"""milestone_unreconciled disposition columns (resolved_at / disposition / resolution_reason / actor)

snowlinedev/Snowline#248: unreconciled flags gain a triage disposition so an agent
can close one (keep_row | replay_transition | dismiss) with a recorded reason.
Additive and nullable; closed flags stay as local triage history (not replicated).

Revision ID: d0e2f4a6b8c1
Revises: c9d1e3f5a7b9
Create Date: 2026-10-09

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0e2f4a6b8c1"
down_revision: str | None = "c9d1e3f5a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLS = (
    ("resolved_at", sa.DateTime()),
    ("disposition", sa.String()),
    ("resolution_reason", sa.Text()),
    ("actor", sa.String()),
)


def upgrade() -> None:
    for name, type_ in _COLS:
        op.add_column("milestone_unreconciled", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLS):
        op.drop_column("milestone_unreconciled", name)
