"""replication stream adoption (SDK tables + conflict state)

Governance adopts the #77 replication stream contract (replication-continuity
§4 / §9 item 3, issue #79):

  * The FIVE SDK-owned `ReplicationBase` tables (subscriptions, outbox, stream
    counters, inbound streams/watermarks, parked events) migrate into
    governance's OWN database via the SDK's metadata — the sanctioned adoption
    shape (the SDK owns the table SHAPE, the plugin's alembic chain owns the
    migration; snowline_plugin_sdk.replication.models docstring).

  * Two GOVERNANCE-owned conflict-state tables (spec §6/§6.1):
    `decision_concurrences` (the concurrent-sibling markers the `unreconciled`
    view derives from) and `replication_lww_registers` (the last-writer-wins
    coordinates that make a same-object race resolve identically on both
    sides). Domain tables on governance's `Base` — they travel in a §7
    `pg_dump`, unlike the SDK plumbing the seed scrubs.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-07-04

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from snowline_plugin_sdk.replication.models import ReplicationBase


revision: str = "f5a6b7c8d9e0"
down_revision: str | None = "e4f5a6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The SDK replication tables, created from the SDK's own metadata so the
    # shape can never drift from the models the emit/ingest modules run on.
    ReplicationBase.metadata.create_all(bind=op.get_bind())

    op.create_table(
        "decision_concurrences",
        # Normalized unordered pair (lesser UUID first) — soft refs into
        # `decisions` (members may arrive in either order across two streams).
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("concurrent_with_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("decision_id", "concurrent_with_id"),
    )

    op.create_table(
        "replication_lww_registers",
        sa.Column("object_kind", sa.String(), nullable=False),
        sa.Column("object_id", sa.Uuid(), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("written_at", sa.DateTime(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("event_ref", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("object_kind", "object_id", "field"),
    )


def downgrade() -> None:
    op.drop_table("replication_lww_registers")
    op.drop_table("decision_concurrences")
    ReplicationBase.metadata.drop_all(bind=op.get_bind())
