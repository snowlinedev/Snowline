"""artifacts.retirement_reason (successor-less retirement, #174)

Generalizes #166's artifact-level supersession: retirement is the STATE
(`superseded_at` non-NULL) and the successor pointer is one form of it — this
column carries the other form, the reason string for docs that retire with no
natural successor (completed checklists, extracted packages, point-in-time
audits — the turtletracks consolidation pilot found ~19/50 such candidates).

Additive nullable column on `artifacts` — a NEW migration chained off the
current head, never an edit to the old one.

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
Create Date: 2026-07-25

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e0f1a2b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("retirement_reason", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("artifacts", "retirement_reason")
