"""replication tables: adopt the SDK replication fabric (#80)

Memory opts into plugin-owned event replication (replication-continuity §4/§9
item 4): the SDK owns the outbox/watermark/parked-event SHAPE (`ReplicationBase`
metadata), the plugin's alembic chain owns creating those tables in memory's OWN
database. Created straight from `ReplicationBase.metadata` so this migration can
never drift from the SDK models (no hand-copied DDL) — the tables land with their
Postgres JSONB columns via the models' JSON/JSONB variant.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-04

"""
from collections.abc import Sequence

from alembic import op

from snowline_plugin_sdk.replication.models import ReplicationBase


revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    ReplicationBase.metadata.create_all(op.get_bind())


def downgrade() -> None:
    ReplicationBase.metadata.drop_all(op.get_bind())
