"""milestone registry

The platform's SECOND identity primitive, beside scopes (milestones.md §2, §9):
the `milestones` table (anchor FK + slash-free name, unique per anchor including
tombstones + lifecycle status/timestamps + `merged_into_id` alias column), an
append-only `milestone_transitions` log, and the `milestone_dependencies` edge
table. The dependency table's SCHEMA lands now so the edge verbs (a later
increment) need no second migration — nothing writes it yet.

Revision ID: d4e6f8a0b2c4
Revises: c3d9f1a2b4e6
Create Date: 2026-07-23

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d4e6f8a0b2c4"
down_revision: str | None = "c3d9f1a2b4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "milestones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("anchor_scope_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("achieved_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("merged_into_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["anchor_scope_id"], ["scopes.id"]),
        sa.ForeignKeyConstraint(["merged_into_id"], ["milestones.id"]),
        sa.PrimaryKeyConstraint("id"),
        # Unique per anchor INCLUDING tombstones (§2): a tombstone stays a row
        # with `merged_into_id` set, so a plain unique constraint reserves a
        # merged-away name forever.
        sa.UniqueConstraint(
            "anchor_scope_id", "name", name="uq_milestone_anchor_name"
        ),
    )
    op.create_table(
        "milestone_transitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("milestone_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(), nullable=False),
        sa.Column("to_status", sa.String(), nullable=False),
        sa.Column(
            "authored_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "milestone_dependencies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dependent_id", sa.Uuid(), nullable=False),
        sa.Column("dependency_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["dependent_id"], ["milestones.id"]),
        sa.ForeignKeyConstraint(["dependency_id"], ["milestones.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dependent_id", "dependency_id", name="uq_milestone_dependency_edge"
        ),
    )


def downgrade() -> None:
    op.drop_table("milestone_dependencies")
    op.drop_table("milestone_transitions")
    op.drop_table("milestones")
