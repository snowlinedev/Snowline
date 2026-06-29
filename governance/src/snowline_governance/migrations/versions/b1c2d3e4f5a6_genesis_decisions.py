"""genesis: decisions

Governance's first persisted table — the decision graph (governance-plugin spec
§4). Columns/types are kept COMPATIBLE with the frozen monolith's `decisions`
table so existing decision rows import cleanly into a running governance instance
later (spec §9). THE ONE DELTA from the monolith schema: `scope_id` is NOT a
ForeignKey into `scopes` here — scopes are platform-owned and live in a different
database, so the scope link is a SOFT reference (`scope_id` + denormalized
`scope_slug`), not a cross-service FK (spec §3). `supersedes_id` IS a self-FK
(intra-table) and NON-unique, forming the branching supersession DAG.

The shadow-graph + artifact tables land in later increments toward issue #5.

Revision ID: b1c2d3e4f5a6
Revises:
Create Date: 2026-06-29

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b1c2d3e4f5a6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        # Soft scope reference (platform-owned scope; no cross-service FK).
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("scope_slug", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("rationale", sa.String(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Self-FK supersession DAG (intra-table, non-unique → branching).
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        # Display-only shadow-graduation provenance (carried for schema-compat;
        # unused this increment — never a FK into shadow).
        sa.Column("shadow_origin_node_id", sa.String(), nullable=True),
        sa.Column("shadow_origin_label", sa.String(), nullable=True),
        sa.Column("shadow_origin_kind", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["supersedes_id"], ["decisions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # The per-scope read/leaf queries filter on the soft scope reference.
    op.create_index(
        "ix_decisions_scope_slug", "decisions", ["scope_slug"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_decisions_scope_slug", table_name="decisions")
    op.drop_table("decisions")
