"""milestones.title — a human-friendly display title beside the slug identity

snowlinedev/Snowline#156: an editable, nullable display title (<= 120 chars,
trimmed, empty -> NULL — enforced in the service). Additive; the slug/address
stays the identity. Rides the DESCRIPTIVE replication register (§9).

Revision ID: e1f3a5b7c9d2
Revises: d0e2f4a6b8c1
Create Date: 2026-10-09

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f3a5b7c9d2"
down_revision: str | None = "d0e2f4a6b8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("milestones", sa.Column("title", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("milestones", "title")
