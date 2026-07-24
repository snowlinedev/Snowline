"""The platform's persisted models — currently just `Scope`, the shared spine.

The platform OWNS the scope namespace (architecture.md §2): identity + the
`parent_id` tree + the `isolated` flag. Every plugin references scopes by slug
and reads the tree from the platform; plugins never own it.

One `Base` / one metadata for the platform — scopes are its first persisted data,
and the migration chain targets this metadata. The `Scope` schema is kept
**compatible with the frozen monolith's `scopes` table** (already trimmed to
identity + tree + isolation in monolith #650) so existing scope rows import
cleanly into a running platform instance later (spec §2, §6). PM/governance
freight that the monolith moved to side tables (external_key, code_sweep,
roadmap_position, spec_repo) does NOT live here — it belongs to the plugins.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


class Scope(Base):
    """A namespace node — the universal primitive every capability hangs off.

    Schema-compatible with the monolith's trimmed-core `scopes` table:
    `id`/`slug` (unique)/`name`/`kind`/`parent_id` (self-FK)/`status`/`isolated`/
    `created_at`/`updated_at`. `parent_id` is the AUTHORITATIVE tree edge (not the
    slug prefix), kept consistent with the slug hierarchy on create; `isolated` is
    the inheritance boundary governance reasons about.
    """

    __tablename__ = "scopes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # org|project|component|topic|initiative
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scopes.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    isolated: Mapped[bool] = mapped_column(
        default=False, server_default=false()
    )  # the boundary for inherited applicability (spec §2.7)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    parent: Mapped["Scope | None"] = relationship(remote_side=[id])


class Milestone(Base):
    """A verifiable, integrated delivery checkpoint — the platform's SECOND
    identity primitive, built in the same shape as `Scope` (milestones.md §2).

    A milestone is the platform-owned, resolvable identity that PM tags,
    governance stamps, and marketing follow-through all reference by ADDRESS and
    read from the registry; plugins never own it. Identity is the canonical
    address `(anchor slug, name)`:

    - `anchor_scope_id` — FK to a `Scope` whose slug is 1 OR 2 segments (org- or
      repo-level; enforced in the service by segment count). The anchor is the
      namespace + resolution home, NOT a membership fence — work from any scope
      may contribute (membership lives in PM, §6.2).
    - `name` — SLASH-FREE slug, canonical lowercase (case-insensitive input, the
      #134/#139 convention composing with scope-slug folding). Unique within its
      anchor scope, **including merge tombstones** (§7) — a merged-away name is
      reserved forever. The `(anchor_scope_id, name)` unique constraint holds that
      across live rows and tombstones alike (a tombstone stays a row here).
    - `outcome` — the outcome statement / exit-criteria prose (§2). Machine/human
      criteria are NOT modeled here — they are PM work items (§6.2).
    - `status` — planned | active | achieved | cancelled, with `activated_at` /
      `achieved_at` / `cancelled_at` and optional `target_date`. Transitions are
      EXPLICIT verbs (the service); nothing is automatic (§2, §4).
    - `merged_into_id` — self-FK alias tombstone (§7). The COLUMN lands now so the
      merge verb (a later increment) needs no second migration; it is always NULL
      until then.
    """

    __tablename__ = "milestones"
    __table_args__ = (
        # Unique per anchor INCLUDING tombstones (§2) — a tombstone is a live row
        # here with `merged_into_id` set, so a plain unique constraint reserves a
        # merged-away name forever without a partial index.
        UniqueConstraint("anchor_scope_id", "name", name="uq_milestone_anchor_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    anchor_scope_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scopes.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="planned"
    )  # planned|active|achieved|cancelled
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("milestones.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    anchor: Mapped["Scope"] = relationship()
    merged_into: Mapped["Milestone | None"] = relationship(remote_side=[id])


class MilestoneTransition(Base):
    """An append-only lifecycle-transition record (milestones.md §2 "Transition
    log"): from/to status, an `authored_at` timestamp (the replication LWW clock,
    §9), and an optional free-text `reason` (where the lifecycle verbs' `reason`
    lands). One row per `activate` / `achieve` / `cancel`; never updated or
    deleted — the audit + the eventual replication source PM subscribes to (§5).
    """

    __tablename__ = "milestone_transitions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    milestone_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("milestones.id"), nullable=False
    )
    from_status: Mapped[str] = mapped_column(String, nullable=False)
    to_status: Mapped[str] = mapped_column(String, nullable=False)
    authored_at: Mapped[datetime] = mapped_column(server_default=func.now())
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    milestone: Mapped["Milestone"] = relationship()


class MilestoneDependency(Base):
    """A milestone→milestone dependency edge (milestones.md §2 `depends_on`): the
    `dependent` milestone depends on the `dependency` milestone.

    The SCHEMA lands in this increment so the edge verbs (`add_dependency` /
    `remove_dependency`, the cycle guard, readiness surfacing) need no second
    migration — but NOTHING writes this table yet; the verbs are a later
    increment (spec §4/§10). Cross-anchor edges are allowed (the anchor is not a
    fence); the cycle guard runs over the global edge set (§2).
    """

    __tablename__ = "milestone_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "dependent_id", "dependency_id", name="uq_milestone_dependency_edge"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dependent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("milestones.id"), nullable=False
    )
    dependency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("milestones.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
