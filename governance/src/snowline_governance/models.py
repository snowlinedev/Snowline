"""Governance's persisted models — `Decision` plus the ARTIFACT graph
(`Artifact` / `ArtifactVersion` / `ArtifactGoverns`).

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
The shadow-graph tables land in a later increment (spec §4).

THE ARTIFACT GRAPH (this increment, spec §4 / §6.3) — `Artifact`,
`ArtifactVersion`, `ArtifactGoverns` — is kept schema-compatible with the
monolith's tables (a lift, per the develop-in-public carve), with ONE structural
delta mirroring `Decision`: `ArtifactGoverns` references its scope by a SOFT
reference (`scope_id` + denormalized `scope_slug`), NOT the monolith's
`ForeignKey("scopes.id")` — scopes are platform-owned and live in another DB. The
governs edge keys on the STABLE `scope_id` (the decision-keying lesson #11), so a
platform-side slug rename can't make a pre-rename governs edge invisible.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --- artifact value sets (single source of truth, carried from the monolith's
# `models_core`; the inline backend is the only one this increment supports —
# git needs a repo registry that's a GitHub-plugin concern, see `artifacts.py`).
ARTIFACT_BACKEND_GIT = "git"
ARTIFACT_BACKEND_INLINE = "inline"
ARTIFACT_BACKENDS = (ARTIFACT_BACKEND_GIT, ARTIFACT_BACKEND_INLINE)
DEFAULT_ARTIFACT_BACKEND = ARTIFACT_BACKEND_INLINE
ARTIFACT_DOC_KINDS = ("spec", "plan", "reference")
ARTIFACT_MATURITIES = ("draft", "exploratory", "stable")
ARTIFACT_VERSION_STATUSES = ("proposed", "superseded")
ARTIFACT_RELATIONS = ("refines", "pivot")
DEFAULT_ARTIFACT_MATURITY = "draft"
DEFAULT_ARTIFACT_VERSION_STATUS = "proposed"


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


class Artifact(Base):
    """A scoped, versioned governing document (spec §4 / §6.3) — the same
    governance NODE shape as the monolith's `Artifact`: identity, `doc_kind`
    (spec/plan/reference), content `backend`, `maturity` (draft→exploratory→
    stable, a descriptor not a gate), the `governs` mapping (`ArtifactGoverns`
    rows + the all-scopes `governs_all` flag), and the version DAG. Its CONTENT
    lives on the versions, not here; the current version is the DERIVED leaf
    (nothing supersedes it), not stored, to avoid a circular FK.

    Schema-compatible with the monolith. THIS increment only writes the INLINE
    backend (content in the version's `body_snapshot`); `repo`/`path` are the
    git-backend locator, NULLABLE and unused here (the git backend needs a repo
    registry that's a GitHub-plugin concern). The monolith's partial unique index
    on `(lower(repo), path) WHERE backend='git'` is carried for schema-compat —
    it constrains git rows only, so inline-only writes never trip it.
    """

    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    doc_kind: Mapped[str] = mapped_column(String, nullable=False)
    backend: Mapped[str] = mapped_column(
        String, nullable=False,
        server_default=DEFAULT_ARTIFACT_BACKEND, default=DEFAULT_ARTIFACT_BACKEND,
    )
    # Git-backend content locator (NULLABLE — populated only for backend='git',
    # which is out of scope this increment). An inline artifact leaves both NULL.
    repo: Mapped[str | None] = mapped_column(String, nullable=True)
    path: Mapped[str | None] = mapped_column(String, nullable=True)
    maturity: Mapped[str] = mapped_column(
        String, nullable=False, server_default=DEFAULT_ARTIFACT_MATURITY,
        default=DEFAULT_ARTIFACT_MATURITY,
    )
    # All-scopes governance: a portfolio-wide reference governs every scope
    # without enumerating them. Mutually exclusive with `ArtifactGoverns` rows
    # (setting `*` clears the rows).
    governs_all: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        Index(
            "uq_artifacts_repo_ci_path", text("lower(repo)"), "path",
            unique=True,
            postgresql_where=text("backend = 'git'"),
        ),
    )


class ArtifactVersion(Base):
    """A milestone snapshot of an `Artifact` (spec §4) — the version DAG. For an
    inline artifact the snapshot IS the content (`body_snapshot`); the git-backend
    locator (`git_ref`/`git_sha`) is carried for schema-compat but NULL on inline
    versions (git is out of scope this increment).

    `supersedes_id` is a nullable, intentionally **non-UNIQUE** self-FK — two
    versions may supersede the same prior, forming the branching DAG (mirrors
    `Decision.supersedes_id`); the artifact's *current* version is the leaf.
    `relation` labels a successor `refines`/`pivot`; `status` is `proposed` until
    `resolve_artifact` flips a losing competing leaf to `superseded`.
    """

    __tablename__ = "artifact_versions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # non-UNIQUE on purpose — branching DAG (mirrors decisions.supersedes_id).
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifact_versions.id"), nullable=True
    )
    relation: Mapped[str | None] = mapped_column(String, nullable=True)
    git_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    git_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    body_snapshot: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=DEFAULT_ARTIFACT_VERSION_STATUS,
        default=DEFAULT_ARTIFACT_VERSION_STATUS,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ArtifactGoverns(Base):
    """The scopes an artifact governs (spec §4, multi-scope). THE STRUCTURAL DELTA
    from the monolith's `ArtifactGoverns`: the scope end is a SOFT reference
    (`scope_id` + denormalized `scope_slug`), NOT a `ForeignKey("scopes.id")` —
    scopes are platform-owned, in another DB (spec §3, mirrors `Decision`). The
    edge keys on the STABLE `scope_id` (#11): a platform-side slug rename can't
    orphan a pre-rename governs edge; `scope_slug` is denormalized so a read can
    label the edge without a round-trip to the platform.

    Composite PK `(artifact_id, scope_id)`. The artifact FK CASCADEs (the edge is
    meaningless without the artifact); the scope end can't CASCADE (no FK across
    the service boundary). The all-scopes form is the separate `governs_all` flag,
    NOT a row here — the two are mutually exclusive.
    """

    __tablename__ = "artifact_governs"

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), primary_key=True
    )
    # SOFT scope reference (platform-owned scope; no cross-service FK). Keyed on
    # the stable id; the slug is denormalized for display.
    scope_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    scope_slug: Mapped[str] = mapped_column(String, nullable=False)
