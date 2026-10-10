"""milestone release-line rank (`line_rank`) + reserve the name `line`

release-line.md §2/§3 (snowlinedev/Snowline#265): each milestone gains a
nullable fractional `line_rank` ordering its anchor's release line (NULL = not
in the line). Unbounded NUMERIC so ordering is numeric in SQL and Python; the
wire form is a string.

The new `POST|DELETE /milestones/{address}/line` suffix route makes `line` a
RESERVED milestone name (milestones.md §2). A row already named `line` would
become unaddressable (every `get`/`resolve` validates the name), so the upgrade
FAILS LOUDLY — naming every such row — rather than silently stranding it. The
check covers tombstones too: a tombstoned `line` is still an address consumers
may have stored, and it would stop resolving just the same. Rename/merge the row
by hand, then re-run the upgrade.

Revision ID: b8c0d2e4f6a8
Revises: a7b9c1d3e5f8
Create Date: 2026-10-09

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c0d2e4f6a8"
down_revision: str | None = "a7b9c1d3e5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    clashing = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT s.slug || '/' || m.name AS address, "
                "m.merged_into_id IS NOT NULL AS tombstone "
                "FROM milestones m JOIN scopes s ON s.id = m.anchor_scope_id "
                "WHERE m.name = 'line' ORDER BY 1"
            )
        )
        .all()
    )
    if clashing:
        listing = ", ".join(
            f"{r.address}{' (merge tombstone)' if r.tombstone else ''}"
            for r in clashing
        )
        raise RuntimeError(
            "cannot reserve the milestone name 'line' (release-line.md §2.2): "
            f"existing milestone(s) already use it: {listing}. The new "
            "/milestones/{address}/line route would make them unaddressable. "
            "Rename or merge them first, then re-run the upgrade."
        )
    op.add_column("milestones", sa.Column("line_rank", sa.Numeric(), nullable=True))


def downgrade() -> None:
    op.drop_column("milestones", "line_rank")
