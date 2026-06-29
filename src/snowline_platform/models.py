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
from datetime import datetime

from sqlalchemy import ForeignKey, String, false, func
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
