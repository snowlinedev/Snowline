"""Governance's persisted models — this increment: just `Decision`.

Governance owns the DECISION GRAPH (its own DB); the platform owns SCOPES. So a
decision references its scope by a SOFT reference (slug + id strings), NOT a
cross-service foreign key — governance cannot FK into a table it doesn't own and
that lives in a different database (governance-plugin spec §3: "a soft reference,
not a cross-service FK").

The `Decision` schema is kept COMPATIBLE with the frozen monolith's `decisions`
table (spec §4 + §9) so existing decision rows migrate cleanly into a running
governance instance later. The monolith columns carried unchanged:
`id`, `scope_id`, `decision`, `rationale`, `recorded_at`, `supersedes_id`
(self-FK, NON-unique → a branching supersession DAG), and the display-only
shadow-graduation provenance markers (`shadow_origin_node_id` /
`shadow_origin_label` / `shadow_origin_kind`). The one schema DELTA from the
monolith: `scope_id` is NOT a `ForeignKey("scopes.id")` here (there is no
`scopes` table in this DB — scopes live in the platform), and a denormalized
`scope_slug` is stored alongside so a read needs no round-trip to the platform
just to label a row with the scope it belongs to.

One `Base` / one metadata for governance — decisions are its first persisted
data, and its alembic chain targets THIS metadata (separate from the platform's).
The shadow-graph + artifact tables land in later increments (spec §4).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Decision(Base):
    """A design/planning decision recorded against a scope.

    Schema-compatible with the monolith's `decisions` table. `scope_id` /
    `scope_slug` are a SOFT reference to a platform-owned scope (no cross-service
    FK). `supersedes_id` is a nullable self-FK populated by `supersede_decision`,
    forming a branching supersession DAG (NON-unique → ≥2 decisions may supersede
    one). The default read filter returns only chain leaves (decisions nothing
    supersedes); predecessors stay in the table for audit.
    """

    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # SOFT scope reference (platform-owned scope; no FK across the service/DB
    # boundary). `scope_id` keeps the monolith column name + UUID type for
    # schema-compat / clean import; `scope_slug` is denormalized so a read can
    # label the row without calling the platform.
    scope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    scope_slug: Mapped[str] = mapped_column(String, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str | None] = mapped_column(String, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("decisions.id"), nullable=True
    )
    # Display-only shadow-graduation provenance (monolith §5). Carried for
    # schema-compat; this increment never writes them (shadow lands later). A
    # bare marker, never a FK into shadow (the inward-only isolation invariant).
    shadow_origin_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    shadow_origin_label: Mapped[str | None] = mapped_column(String, nullable=True)
    shadow_origin_kind: Mapped[str | None] = mapped_column(String, nullable=True)
