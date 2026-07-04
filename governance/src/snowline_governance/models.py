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

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
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

# --- shadow value sets (carried from the monolith's `models_core`; §4 / §6.4).
# A branch is archived (a status flip that KEEPS the row, listable) — never
# expired/deleted.
SHADOW_BRANCH_STATUSES = ("active", "archived")
DEFAULT_SHADOW_BRANCH_STATUS = "active"
# Named because it is LOAD-BEARING contract in three places that must
# coincide: the append guard (add_message 409s), the thread page's `flags`
# entry, and the manifest composer's `disabled_when` — one constant, not
# three bare literals that can desync.
SHADOW_BRANCH_STATUS_ARCHIVED = "archived"


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


# --- the SHADOW / speculation graph (spec §4 / §5 / §6.4) -------------------
#
# A second governance subgraph: named speculative branches per scope, speculative
# decision nodes, the inward-only citation edge, and a durable per-branch
# conversation log. Carried (functionality-first, NOT imported) from the frozen
# monolith's `models_core` (`ShadowBranch` / `ShadowNode` / `ShadowNodeCitation` /
# `ShadowConversationEvent`), schema-compatible so existing shadow rows migrate
# cleanly later.
#
# THE STRUCTURAL ISOLATION INVARIANT (spec §6.4, decision 8a7f0a11 — "inward
# only"): references flow ONE WAY only. A shadow row may reference a real
# `decisions` row, but NOTHING real ever references a shadow row. Two structural
# deltas from the monolith carry this here:
#
#   1. `ShadowBranch.scope_id` is a SOFT scope reference (`scope_id` +
#      denormalized `scope_slug`), NOT a `ForeignKey("scopes.id")` — scopes are
#      platform-owned and live in another DB (mirrors `Decision`/`ArtifactGoverns`).
#
#   2. `ShadowNodeCitation.cited_decision_id` and `ShadowNode.graduated_decision_id`
#      store the real decision's id as a PLAIN VALUE — NO `ForeignKey("decisions.id")`.
#      The monolith FKs these (a shadow→real FK is the permitted inward direction
#      there), but here the inward-only invariant is held STRUCTURALLY: there is
#      NO foreign key from any shadow table to a real `decisions`/`artifacts` row
#      in either direction, so the real graph is provably independent of shadow at
#      the schema level. The target's existence is validated at the SERVICE layer
#      (`shadow.add_citation`) instead of by an FK. Intra-shadow FKs
#      (branch→node→citation) remain, since those are within the shadow subgraph.


class ShadowBranch(Base):
    """A named speculative branch within a scope (spec §4 / §6.4).

    Multiple rival branches may be held per scope ("auth as X" vs "auth as Y"),
    each killable independently. Addressed `<scope>:<name>` — the name is unique
    WITHIN its scope (the `uq_shadow_branch_scope_name` constraint), no global
    namespace. Carries a running `narrative_notes` doc (the reasoning thread that
    reconstructs *how we got here* on re-entry) and a set of `ShadowNode`s.

    Schema-compatible with the monolith's `ShadowBranch`, with the ONE delta
    mirroring `Decision`: `scope_id` is a SOFT reference to a platform-owned scope
    (`scope_id` + denormalized `scope_slug`), NOT a `ForeignKey("scopes.id")`.
    """

    __tablename__ = "shadow_branches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # SOFT scope reference (platform-owned scope; no cross-service FK). Keyed on
    # the stable id; the slug is denormalized so a read labels the branch without
    # a round-trip to the platform.
    scope_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    scope_slug: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # The per-branch narrative-notes doc: a single mutable body, NULL until first
    # written. Nodes are the verdicts; the notes are the curated reasoning thread.
    narrative_notes: Mapped[str | None] = mapped_column(String, nullable=True)
    # The Agent SDK session id backing this branch's conversation — persisted so a
    # later visit resumes the same agent context. NULL until the first turn runs.
    agent_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False,
        server_default=DEFAULT_SHADOW_BRANCH_STATUS,
        default=DEFAULT_SHADOW_BRANCH_STATUS,
    )  # see SHADOW_BRANCH_STATUSES
    # When the branch was first archived (the active→archived transition, §6.4).
    # NULL while active; pinned to the original archival across an idempotent
    # re-archive (deliberately NOT `updated_at`).
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("scope_id", "name", name="uq_shadow_branch_scope_name"),
    )


class ShadowNode(Base):
    """A speculative-decision node within a shadow branch (spec §4).

    The unit of speculation stays the decision; a node is a not-yet-real decision
    carrying its own crisp `rationale`, individually addressable so a later
    graduation can cherry-pick ONE node. The field shape mirrors `Decision`
    (`statement` ↔ `Decision.decision`, `rationale`) so graduation's translation
    into a real decision is a straight copy. Deleting the branch cascades.

    Schema-compatible with the monolith's `ShadowNode`, with the STRUCTURAL DELTA:
    `graduated_decision_id` stores the real decision's id as a PLAIN VALUE, NOT a
    `ForeignKey("decisions.id")` — the inward-only invariant held structurally (no
    shadow→real FK in this DB). NULL while the node is still speculative;
    graduation (a later PR) sets it. The branch FK is intra-shadow and stays.
    """

    __tablename__ = "shadow_nodes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_branches.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    statement: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str | None] = mapped_column(String, nullable=True)
    # The real decision this node graduated into (§4), or NULL while speculative.
    # A PLAIN VALUE, not an FK — the inward-only invariant is structural here (no
    # shadow→real FK), unlike the monolith's `ForeignKey("decisions.id")`.
    # Graduation (a later PR) populates it.
    graduated_decision_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class ShadowNodeCitation(Base):
    """A citation FROM a shadow node (spec §4 / §6.4) — the structural carrier of
    the inward-only rule.

    Exactly one target (XOR, the `ck_shadow_citation_one_target` check): another
    shadow node in the SAME branch (`cited_node_id` — a within-shadow dependency)
    OR a real decision (`cited_decision_id` — the permitted INWARD reference).
    There is deliberately no citation edge running the other way anywhere in the
    schema; that asymmetry IS the inward-only isolation invariant (§6.4).

    THE STRUCTURAL DELTA from the monolith: `cited_decision_id` stores the real
    decision's id as a PLAIN VALUE, NOT a `ForeignKey("decisions.id")` — NO shadow
    table FKs into a real row, in either direction. The real decision's existence
    is validated at the SERVICE layer (`shadow.add_citation`). `cited_node_id`
    stays an intra-shadow FK (within the shadow subgraph). De-duplication of
    (node → target) pairs is enforced by partial unique indexes (a plain
    UniqueConstraint can't, since NULLs compare distinct).
    """

    __tablename__ = "shadow_node_citations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_nodes.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Inward target A: another shadow node (within-shadow dependency). Intra-shadow
    # FK — stays, it points within the shadow subgraph.
    cited_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("shadow_nodes.id", ondelete="CASCADE"), nullable=True
    )
    # Inward target B: a real decision (shadow→real is the permitted direction;
    # the reverse never is). A PLAIN VALUE, NOT an FK — the inward-only invariant
    # is structural (no shadow→real FK). Existence validated in `add_citation`.
    cited_decision_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "(cited_node_id IS NULL) <> (cited_decision_id IS NULL)",
            name="ck_shadow_citation_one_target",
        ),
        Index(
            "uq_shadow_citation_node",
            "node_id", "cited_node_id",
            unique=True,
            postgresql_where=text("cited_node_id IS NOT NULL"),
        ),
        Index(
            "uq_shadow_citation_decision",
            "node_id", "cited_decision_id",
            unique=True,
            postgresql_where=text("cited_decision_id IS NOT NULL"),
        ),
    )


class ShadowConversationEvent(Base):
    """One event in a branch's DURABLE, append-only conversation log (spec §4).

    A turn's `user` message and each agent event is a row, ordered by a per-branch
    monotonic `seq` (which doubles as the SSE resume cursor). The turn runs
    server-side and appends here, so the conversation outlives any single client
    connection. Cascades with the branch.

    Isolated by construction like the rest of the shadow tables: nothing real
    references it (no real→shadow FK). Carried for schema-compat; this increment
    does not yet run agent turns (the conversation/turn machinery is a later PR).
    """

    __tablename__ = "shadow_conversation_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_branches.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Per-branch monotonic sequence — ordering AND the SSE resume cursor.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # The event's `type` — a plain string, unschematized like the payload.
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # The full normalized event dict the surface renders (untyped JSONB).
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("branch_id", "seq", name="uq_shadow_conv_branch_seq"),
    )


# --- REPLICATION conflict state (replication-continuity §6/§6.1, #79) --------
#
# GOVERNANCE-OWNED domain tables (on `Base`, NOT the SDK's `ReplicationBase`):
# they carry domain meaning — which decisions collided, which register value is
# current — and must travel with the store in a §7 `pg_dump`, unlike the SDK's
# envelope plumbing that the seed scrubs.


class DecisionConcurrence(Base):
    """One §6.1 concurrent-sibling flag: two decisions authored on opposite sides
    of a partition, in overlapping scope (the applicability chain). Written at
    INGEST by the detection walk (`replication_apply`), symmetrically on both
    instances — each side derives the same pair from the same causal facts, so
    the markers converge without replicating themselves.

    The pair is stored NORMALIZED (`decision_id` < `concurrent_with_id` as
    UUIDs), one row per unordered pair — both sides compute the identical row.
    The row is never deleted: "unreconciled" is DERIVED (both members still
    leaves), so recording the reconciling supersession clears the flag on both
    sides the moment that ordinary event applies — no marker write needed
    (§6.1: reconciliation is ordinary governance)."""

    __tablename__ = "decision_concurrences"

    # Normalized pair: decision_id is the lesser UUID, concurrent_with_id the
    # greater — plain values (soft refs into `decisions`; the members may arrive
    # in either order across the two streams, so no FK ordering assumption).
    decision_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    concurrent_with_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LwwRegister(Base):
    """The §6 last-writer-wins state for ONE mutable field of one row — the
    "contested row" bookkeeping that lets both sides resolve a same-object race
    as a pure function of the two events (LWW by event timestamp, `source_id`
    tiebreak), regardless of arrival order.

    Written by LOCAL lifecycle writes (via `replication_stream.emit`) and by
    WINNING applies (`replication_apply`): `(written_at, source_id)` is the
    current value's authoring coordinate, `event_ref` the authoring event's
    `event_id` — the id a resolved-conflict WARNING pairs with the incoming
    event's (§6: logged with BOTH event ids). Only the LWW-register event types
    (notes/maturity/governs/graduation stamps) keep registers; append-only
    events need none."""

    __tablename__ = "replication_lww_registers"

    object_kind: Mapped[str] = mapped_column(String, primary_key=True)
    object_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    field: Mapped[str] = mapped_column(String, primary_key=True)
    written_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    event_ref: Mapped[str] = mapped_column(String, nullable=False)


# --- the WEBHOOK BUS (spec §7) — the EMIT side ------------------------------
#
# Governance emits signed `decision.recorded` / `decision.superseded` events on a
# webhook bus (HMAC-SHA256 over the raw body; `contract_version` in the payload;
# a transactional outbox + async delivery with a per-subscription monotonic
# `seq`). Carried (functionality-first, NOT imported) from the frozen monolith's
# `snowline_substrate.models_core` (`WebhookSubscription` / `WebhookDelivery`),
# schema-compatible so monolith rows migrate cleanly later (spec §9). The
# delivery machinery itself lives in `replication.py` (httpx is kept out of the
# model layer, mirroring the monolith's substrate/server split).
#
# THE ONE STRUCTURAL DELTA from the monolith, mirroring `Decision`: the
# subscription's optional `scope_id` filter is a SOFT scope reference (no
# `ForeignKey("scopes.id")`) — scopes are platform-owned and live in another DB.
# The filter matches on the STABLE `scope_id` (the decision-keying lesson #11),
# so a platform-side slug rename can't make a pre-rename subscription stop
# matching its scope's decisions.


class WebhookSubscription(Base):
    """A registered webhook subscriber for decision events (spec §7).

    A receiver registers a `target_url` + a shared `secret` (the HMAC-SHA256
    signing key) and the `event_types` it wants (the published set:
    `snowline_governance.contract.EVENT_TYPES`). `scope_id` NULL means GLOBAL —
    every decision matches; a set `scope_id` restricts matching to decisions
    recorded in that one scope (a private scope's decisions need not flow out).
    The filter is a SOFT scope reference matched on the stable `scope_id` (no
    cross-service FK — scopes are platform-owned). Managed PROGRAMMATICALLY (no
    MCP tool / CLI — remote registration is out-of-band v1, per the SDK's
    `events.py` note); see `replication.create_subscription`.
    """

    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    target_url: Mapped[str] = mapped_column(String, nullable=False)
    # The HMAC-SHA256 signing key — every delivery to this subscriber carries an
    # `X-Snowline-Signature: sha256=<hmac>` header the receiver verifies.
    secret: Mapped[str] = mapped_column(String, nullable=False)
    # The event types this subscriber wants — a JSONB list[str].
    event_types: Mapped[list] = mapped_column(JSONB, nullable=False)
    # NULL = global (matches every decision); set = only this scope's decisions.
    # A SOFT scope reference (no FK across the service/DB boundary), matched on
    # the stable id so a platform-side slug rename can't orphan it (#11).
    scope_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WebhookDelivery(Base):
    """A single decision-event delivery — the transactional outbox AND the
    delivery log (spec §7).

    A `WebhookDelivery` row is written in the SAME transaction as the decision it
    carries (transactional outbox: atomic with `record_decision` /
    `supersede_decision`) — but with `seq` still NULL. `seq` is a PER-SUBSCRIPTION
    MONOTONIC, contiguous sequence the receiver orders by (a supersession can't be
    applied before the decision it supersedes), allocated at DELIVERY time by the
    background loop (`max(seq)+1` over this subscription's rows), NOT in the
    decision transaction. That placement is deliberate: the delivery loop is the
    genuine single writer of `seq` (one tick at a time), so `max(seq)+1` is
    race-free THERE — whereas allocating it in the decision txn would couple a seq
    collision to a `record_decision` ROLLBACK. The `(subscription_id, seq)`
    unique constraint makes any gap-or-dup loud, per subscriber.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Per-subscription monotonic sequence (the receiver's ordering key). NULL
    # until the delivery loop allocates it at send time — see the class docstring
    # + `replication.deliver_pending`.
    seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # pending | delivered | failed
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "seq", name="uq_webhook_delivery_subscription_seq"
        ),
    )
