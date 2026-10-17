"""decisions.origin_source (§7.1 union provenance, #98)

The cold-start union's §6.1 detection needs "locally AUTHORED" to be
computable: a backfilled event is concurrent with every locally-authored
decision in an overlapping applicability chain, but a decision that was itself
REPLICATED IN (live or by an earlier backfill) must never count as local — it
would flag against its own author's next event (sequential same-source events
masquerading as siblings). Replication apply stamps the (`#backfill`-stripped)
origin source on every decision it creates; NULL means locally authored.

Additive nullable column — a NEW migration chained off the current head. Rows
replicated in BEFORE this column existed read as locally authored (a
previously-paired reunion should expect, and bulk-resolve, the extra flags —
runbook note); the cold-start case has no such rows.

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-10-16

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "decisions",
        sa.Column("origin_source", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("decisions", "origin_source")
