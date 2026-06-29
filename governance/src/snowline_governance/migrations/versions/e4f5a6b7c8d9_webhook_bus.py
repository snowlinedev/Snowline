"""webhook bus (WebhookSubscription / WebhookDelivery)

The FINAL increment toward issue #5 (governance-plugin spec §7) — the signed
decision-event webhook bus: governance EMITs `decision.recorded` /
`decision.superseded` on a transactional outbox + async delivery with a
per-subscription monotonic `seq`. Columns/types are kept COMPATIBLE with the
frozen monolith's `webhook_subscriptions` / `webhook_deliveries` tables so
existing rows import cleanly later (spec §9).

THE ONE STRUCTURAL DELTA from the monolith, mirroring `decisions`: the
subscription's optional `scope_id` filter is a SOFT scope reference — NO
`ForeignKey("scopes.id")` — scopes are platform-owned and live in another DB.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-06-29

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e4f5a6b7c8d9"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_url", sa.String(), nullable=False),
        sa.Column("secret", sa.String(), nullable=False),
        sa.Column("event_types", postgresql.JSONB(), nullable=False),
        # SOFT scope reference (platform-owned scope; no cross-service FK). NULL
        # = global; set = only this scope's decisions.
        sa.Column("scope_id", sa.Uuid(), nullable=True),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        # Per-subscription monotonic seq — NULL until the delivery loop allocates
        # it at send time (BigInteger, matching the monolith).
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status", sa.String(), server_default="pending", nullable=False
        ),
        sa.Column(
            "attempts", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["webhook_subscriptions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        # Per-subscription monotonic, contiguous sequence — any gap-or-dup is
        # loud, per subscriber.
        sa.UniqueConstraint(
            "subscription_id",
            "seq",
            name="uq_webhook_delivery_subscription_seq",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_subscription_id",
        "webhook_deliveries",
        ["subscription_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_deliveries_subscription_id",
        table_name="webhook_deliveries",
    )
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_subscriptions")
