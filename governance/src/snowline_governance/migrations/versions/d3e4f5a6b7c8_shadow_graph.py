"""shadow graph (ShadowBranch / ShadowNode / ShadowNodeCitation / ShadowConversationEvent)

The third governance increment toward issue #5 (governance-plugin spec §4 / §5 /
§6.4) — the SHADOW / speculation subgraph: named speculative branches per scope,
speculative decision nodes, the inward-only citation edge, and a durable
per-branch conversation log. Columns/types are kept COMPATIBLE with the frozen
monolith's `shadow_branches` / `shadow_nodes` / `shadow_node_citations` /
`shadow_conversation_events` tables so existing shadow rows import cleanly later
(spec §9).

THE STRUCTURAL ISOLATION INVARIANT (spec §6.4, "inward only"): references flow
ONE WAY only — a shadow row may reference a real decision, but NOTHING real ever
references shadow. Two deltas from the monolith carry this:

  - `shadow_branches.scope_id` is a SOFT scope reference (`scope_id` +
    denormalized `scope_slug`), NOT a `ForeignKey("scopes.id")` — scopes are
    platform-owned, in another DB (mirrors `decisions` / `artifact_governs`).

  - `shadow_node_citations.cited_decision_id` and `shadow_nodes.graduated_
    decision_id` store the real decision's id as a PLAIN VALUE — NO
    `ForeignKey("decisions.id")`. There is NO foreign key from any shadow table
    to a real `decisions`/`artifacts` row in either direction; the inward-only
    invariant is held STRUCTURALLY at the schema level, and a citation's real
    target is validated at the service layer. Intra-shadow FKs
    (branch→node→citation) remain.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-06-29

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shadow_branches",
        sa.Column("id", sa.Uuid(), nullable=False),
        # SOFT scope reference (platform-owned scope; no cross-service FK).
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("scope_slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("narrative_notes", sa.String(), nullable=True),
        sa.Column("agent_session_id", sa.String(), nullable=True),
        sa.Column(
            "status", sa.String(), server_default="active", nullable=False
        ),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
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
        # Branch name unique within its scope (§4 addressing `<scope>:<name>`).
        sa.UniqueConstraint(
            "scope_id", "name", name="uq_shadow_branch_scope_name"
        ),
    )
    op.create_index(
        "ix_shadow_branches_scope_id", "shadow_branches", ["scope_id"]
    )

    op.create_table(
        "shadow_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("statement", sa.String(), nullable=False),
        sa.Column("rationale", sa.String(), nullable=True),
        # PLAIN VALUE, not an FK — the inward-only invariant is structural here
        # (no shadow→real FK). Graduation (a later PR) populates it.
        sa.Column("graduated_decision_id", sa.Uuid(), nullable=True),
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
        # Intra-shadow FK (within the shadow subgraph) — stays.
        sa.ForeignKeyConstraint(
            ["branch_id"], ["shadow_branches.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shadow_nodes_branch_id", "shadow_nodes", ["branch_id"])

    op.create_table(
        "shadow_node_citations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        # Inward target A: another shadow node — intra-shadow FK, stays.
        sa.Column("cited_node_id", sa.Uuid(), nullable=True),
        # Inward target B: a real decision — a PLAIN VALUE, NOT an FK (the
        # inward-only invariant is structural). Validated in the service layer.
        sa.Column("cited_decision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["shadow_nodes.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["cited_node_id"], ["shadow_nodes.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # Exactly one target — XOR over the two NULL-tests.
        sa.CheckConstraint(
            "(cited_node_id IS NULL) <> (cited_decision_id IS NULL)",
            name="ck_shadow_citation_one_target",
        ),
    )
    op.create_index(
        "ix_shadow_node_citations_node_id",
        "shadow_node_citations",
        ["node_id"],
    )
    # Partial unique indexes — de-dup (node → target) pairs (a plain
    # UniqueConstraint can't, since NULLs compare distinct).
    op.create_index(
        "uq_shadow_citation_node",
        "shadow_node_citations",
        ["node_id", "cited_node_id"],
        unique=True,
        postgresql_where=sa.text("cited_node_id IS NOT NULL"),
    )
    op.create_index(
        "uq_shadow_citation_decision",
        "shadow_node_citations",
        ["node_id", "cited_decision_id"],
        unique=True,
        postgresql_where=sa.text("cited_decision_id IS NOT NULL"),
    )

    op.create_table(
        "shadow_conversation_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["shadow_branches.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "branch_id", "seq", name="uq_shadow_conv_branch_seq"
        ),
    )
    op.create_index(
        "ix_shadow_conversation_events_branch_id",
        "shadow_conversation_events",
        ["branch_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shadow_conversation_events_branch_id",
        table_name="shadow_conversation_events",
    )
    op.drop_table("shadow_conversation_events")
    op.drop_index(
        "uq_shadow_citation_decision", table_name="shadow_node_citations"
    )
    op.drop_index(
        "uq_shadow_citation_node", table_name="shadow_node_citations"
    )
    op.drop_index(
        "ix_shadow_node_citations_node_id", table_name="shadow_node_citations"
    )
    op.drop_table("shadow_node_citations")
    op.drop_index("ix_shadow_nodes_branch_id", table_name="shadow_nodes")
    op.drop_table("shadow_nodes")
    op.drop_index("ix_shadow_branches_scope_id", table_name="shadow_branches")
    op.drop_table("shadow_branches")
