"""artifact_versions.title (derived display title, #168)

The body's first markdown heading, denormalized at mint so compact-row reads
(`list_artifacts` / `applicable_artifacts`) can label a doc without touching
bodies. A PURE function of `body_snapshot` (`artifacts.derive_title`) — which is
also why the BACKFILL below is safe to run here: deriving from stored bodies
produces exactly what the write path would have stored, on both replication
peers independently.

Additive nullable column on `artifact_versions` (created in c2d3e4f5a6b7) — a
NEW migration chained off the current head, never an edit to the old one.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-07-25

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d9e0f1a2b3c4"
down_revision: str | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifact_versions",
        sa.Column("title", sa.String(), nullable=True),
    )
    # Backfill: derive titles for every existing version from its stored body.
    # Imported at RUN time (not module import — alembic loads version modules
    # before the app package is necessarily importable in offline tooling).
    from snowline_governance.artifacts import derive_title

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, body_snapshot FROM artifact_versions "
            "WHERE body_snapshot IS NOT NULL"
        )
    ).fetchall()
    for vid, body in rows:
        title = derive_title(body)
        if title is not None:
            bind.execute(
                sa.text(
                    "UPDATE artifact_versions SET title = :title WHERE id = :id"
                ),
                {"title": title, "id": vid},
            )


def downgrade() -> None:
    op.drop_column("artifact_versions", "title")
