"""lww tombstones: the per-name last-writer-wins register (#80)

The write-model rework that makes memory replication-safe (replication-continuity
§4 coverage note): `forget` becomes a TOMBSTONE (`forgotten`) instead of a hard
delete, and every write carries an LWW clock (`last_event_at`, `last_source_id`)
so the register named X converges under §6's deterministic merge.

Existing rows: `forgotten` backfills False (all live), and the LWW clock backfills
from each row's `updated_at` (its best available authoring time) with a `legacy`
source id — so any future write, local or replicated, orders correctly against
them. Memories HARD-DELETED under the old `forget` leave no tombstone; that is
fine — nothing references them and no pending `set` for them exists to resurrect,
so there is nothing to converge.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-04

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column(
            "forgotten",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "memories",
        sa.Column(
            "last_event_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "memories",
        sa.Column(
            "last_source_id",
            sa.String(),
            nullable=False,
            server_default="legacy",
        ),
    )
    # Backfill the LWW clock from each row's existing authoring time so historical
    # rows compare correctly against future writes (the added-column default seeds
    # NOT NULL; this replaces it with the real per-row value).
    op.execute("UPDATE memories SET last_event_at = updated_at")


def downgrade() -> None:
    op.drop_column("memories", "last_source_id")
    op.drop_column("memories", "last_event_at")
    op.drop_column("memories", "forgotten")
