"""artifacts.code_refs (structured code anchors, #172)

The spec↔code map: a JSONB list of `{repo?, path, symbol?, note?}` rows naming
the code an artifact grounds in — populated/re-validated by the drift sweep,
queried in reverse by `artifacts_for_path`. NULL = unmapped.

Additive nullable column on `artifacts` (created in c2d3e4f5a6b7) — a NEW
migration chained off the current head, never an edit to the old one.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-07-25

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "e0f1a2b3c4d5"
down_revision: str | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("code_refs", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("artifacts", "code_refs")
