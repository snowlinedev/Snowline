"""artifact_versions.milestone (soft release-correlation ref, #141)

An optional slug stamped verbatim on an `ArtifactVersion` at mint — the
portfolio's cross-plugin release-correlation key (PM tags work items with the
same slug; governance records which artifact VERSION a release shipped as). A
SOFT reference, exactly like `ArtifactGoverns.scope_slug` and PM's `spec_id`: it
names no real scope and is NEVER resolved, so there is no FK — just a nullable
string, grammar-validated + canonical-lowercased at the write boundary.

Indexed so the `list_versions_by_milestone` filter is an equality scan, not a
full-table sweep. Additive nullable column on `artifact_versions` (created in
c2d3e4f5a6b7) — a NEW migration chained off the current head, never an edit to
the old one.

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
Create Date: 2026-07-18

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b7c8d9e0f1a2"
down_revision: str | None = "a6b7c8d9e0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifact_versions",
        sa.Column("milestone", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_artifact_versions_milestone",
        "artifact_versions",
        ["milestone"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_artifact_versions_milestone", table_name="artifact_versions"
    )
    op.drop_column("artifact_versions", "milestone")
