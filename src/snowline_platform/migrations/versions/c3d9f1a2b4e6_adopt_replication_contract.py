"""adopt replication contract

The platform's own opt-in to the replication contract it offers plugins
(replication-continuity spec §8, §9 item 5, issue #81): `scope.created` /
`scope.updated` ride the SAME five SDK-owned tables
(`snowline_plugin_sdk.replication.models`, issue #77) any opted-in plugin
hosts in its OWN database — column-for-column identical to the SDK's
`ReplicationBase` metadata, adopted here into the platform's alembic chain
rather than re-derived.

Revision ID: c3d9f1a2b4e6
Revises: a1b2c3d4e5f6
Create Date: 2026-07-04

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c3d9f1a2b4e6"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on Postgres, plain JSON elsewhere — matches the SDK's `JSONColumn`
# (`snowline_plugin_sdk.replication.models`) exactly.
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "replication_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_url", sa.String(), nullable=False),
        sa.Column("secret", sa.String(), nullable=False),
        sa.Column("event_types", _JSON, nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("epoch", sa.String(), nullable=False),
        sa.Column("peer_source_id", sa.String(), nullable=True),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "epoch", name="uq_replication_sub_stream"
        ),
    )

    op.create_table(
        "replication_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column(
            "status", sa.String(), server_default="pending", nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["replication_subscriptions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id", "seq", name="uq_replication_outbox_sub_seq"
        ),
    )
    op.create_index(
        "ix_replication_outbox_subscription_id",
        "replication_outbox",
        ["subscription_id"],
    )

    op.create_table(
        "replication_stream_counters",
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("epoch", sa.String(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("source_id", "epoch"),
    )

    op.create_table(
        "replication_inbound_streams",
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("epoch", sa.String(), nullable=False),
        sa.Column("secret", sa.String(), nullable=False),
        sa.Column("previous_secret", sa.String(), nullable=True),
        sa.Column("gate_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "applied_seq", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("blocked_seq", sa.BigInteger(), nullable=True),
        sa.Column(
            "blocked_attempts", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("source_id", "epoch"),
    )

    op.create_table(
        "replication_parked_events",
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("epoch", sa.String(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column(
            "parked_at", sa.DateTime(), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("source_id", "epoch", "seq"),
    )


def downgrade() -> None:
    op.drop_table("replication_parked_events")
    op.drop_table("replication_inbound_streams")
    op.drop_table("replication_stream_counters")
    op.drop_index(
        "ix_replication_outbox_subscription_id", table_name="replication_outbox"
    )
    op.drop_table("replication_outbox")
    op.drop_table("replication_subscriptions")
