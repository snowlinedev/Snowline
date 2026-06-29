"""artifacts: artifact graph (Artifact / ArtifactVersion / ArtifactGoverns)

The second governance increment toward issue #5 (governance-plugin spec §4 / §6.3)
— the ARTIFACT graph: governing docs with a version DAG and scope-applicability.
Columns/types are kept COMPATIBLE with the frozen monolith's `artifacts` /
`artifact_versions` / `artifact_governs` tables so existing artifact rows import
cleanly into a running governance instance later (spec §9).

THE ONE DELTA from the monolith schema (mirroring `decisions`): `artifact_governs`
does NOT FK its `scope_id` into a `scopes` table — scopes are platform-owned and
live in a different database, so the scope link is a SOFT reference (`scope_id` +
denormalized `scope_slug`), not a cross-service FK (spec §3). `supersedes_id` on
`artifact_versions` IS a self-FK (intra-table) and NON-unique, forming the
branching version DAG.

This increment writes only the INLINE backend (content on a version's
`body_snapshot`); the git-backend locator columns (`repo`/`path`/`git_ref`/
`git_sha`) and the partial unique index on `(lower(repo), path) WHERE
backend='git'` are carried for schema-compat but unused here.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-29

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("doc_kind", sa.String(), nullable=False),
        sa.Column(
            "backend", sa.String(), server_default="inline", nullable=False
        ),
        # Git-backend locator (NULLABLE — populated only for backend='git').
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("path", sa.String(), nullable=True),
        sa.Column(
            "maturity", sa.String(), server_default="draft", nullable=False
        ),
        sa.Column(
            "governs_all",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id"),
    )
    # Partial unique index — constrains git rows only (schema-compat; inline
    # rows leave repo/path NULL and never trip it).
    op.create_index(
        "uq_artifacts_repo_ci_path",
        "artifacts",
        [sa.text("lower(repo)"), "path"],
        unique=True,
        postgresql_where=sa.text("backend = 'git'"),
    )

    op.create_table(
        "artifact_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        # Self-FK supersession DAG (intra-table, non-unique → branching).
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("relation", sa.String(), nullable=True),
        sa.Column("git_ref", sa.String(), nullable=True),
        sa.Column("git_sha", sa.String(), nullable=True),
        sa.Column("body_snapshot", sa.String(), nullable=True),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column(
            "status", sa.String(), server_default="proposed", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifacts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["supersedes_id"], ["artifact_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_artifact_versions_artifact_id",
        "artifact_versions",
        ["artifact_id"],
        unique=False,
    )

    op.create_table(
        "artifact_governs",
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        # SOFT scope reference (platform-owned scope; no cross-service FK).
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("scope_slug", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifacts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("artifact_id", "scope_id"),
    )


def downgrade() -> None:
    op.drop_table("artifact_governs")
    op.drop_index(
        "ix_artifact_versions_artifact_id", table_name="artifact_versions"
    )
    op.drop_table("artifact_versions")
    op.drop_index("uq_artifacts_repo_ci_path", table_name="artifacts")
    op.drop_table("artifacts")
