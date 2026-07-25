"""artifacts.superseded_by_id / superseded_at (artifact-level supersession, #166)

The consolidate/retire pointer: a retired artifact points at the artifact that
replaced it (a nullable self-FK — many retired docs may point at the one spec
that absorbed them), with `superseded_at` stamping the retirement. NULL = live.
Reads exclude superseded artifacts by default; the pointer keeps the audit
trail ("replaced by X") that `set_governs(None)` never could.

Additive nullable columns on `artifacts` (created in c2d3e4f5a6b7) — a NEW
migration chained off the current head, never an edit to the old one.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-07-25

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c8d9e0f1a2b3"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "artifacts",
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
    )
    op.create_foreign_key(
        "fk_artifacts_superseded_by_id",
        "artifacts",
        "artifacts",
        ["superseded_by_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_artifacts_superseded_by_id", "artifacts", type_="foreignkey"
    )
    op.drop_column("artifacts", "superseded_at")
    op.drop_column("artifacts", "superseded_by_id")
