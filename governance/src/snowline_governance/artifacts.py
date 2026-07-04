"""The artifact substrate — register / revise / resolve / read of governing docs.

Carried (functionality-first, NOT imported) from the frozen monolith's
`snowline_server.artifacts`, de-PM'd and INLINE-ONLY (spec §4 / §6.3, toward
issue #5). An `Artifact` is a governance node (identity + `doc_kind` + content
`backend` + `maturity` + the `governs` mapping); its CONTENT lives on the version
DAG, and the *current* version is the DERIVED leaf (the shared `branching.py`
helper), not stored. `resolve_artifact` collapses competing leaves.

TWO STRUCTURAL CHANGES from the monolith:

  - **Inline-only.** The git backend needs a repo registry + a GitHub-side
    content transport (the monolith's `_git_content_fetcher`/`read_spec`), which
    is a GitHub-plugin concern — out of scope here. `register_artifact` REJECTS a
    `backend='git'` with a clear, actionable error pointing at inline; content
    lives in the substrate on the version's `body_snapshot`. There is no backend
    adapter — an inline version's content IS its `body_snapshot`, so version
    construction sets it directly (no `snapshot_content` dispatch).

  - **Soft scope refs for `governs`.** Scopes live in the PLATFORM, so an
    `ArtifactGoverns` edge stores a SOFT reference (`scope_id` + `scope_slug`),
    keyed on the STABLE `scope_id` (the decision-keying lesson #11) — a
    platform-side slug rename can't orphan a pre-rename edge. The MCP surface
    resolves a slug→`(id, slug)` against the platform's `ScopeClient` BEFORE
    calling `set_governs`/`register_artifact`, exactly as the decision tools do.

`applicable_artifacts(scope)` mirrors `decisions.applicable_decisions`: it asks
the injected `ScopeClient` for the reader's ancestor chain (the platform's
isolation-halting walk) and returns the artifacts governing any chain scope
(matched by the STABLE `scope_id`) PLUS every `governs_all` artifact.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from snowline_governance import branching, replication_stream
from snowline_governance.contract import (
    EVENT_ARTIFACT_GOVERNS_SET,
    EVENT_ARTIFACT_MATURITY_SET,
    EVENT_ARTIFACT_REGISTERED,
    EVENT_ARTIFACT_RESOLVED,
    EVENT_ARTIFACT_REVISED,
)
from snowline_governance.models import (
    ARTIFACT_BACKEND_GIT,
    ARTIFACT_BACKEND_INLINE,
    ARTIFACT_BACKENDS,
    ARTIFACT_DOC_KINDS,
    ARTIFACT_MATURITIES,
    ARTIFACT_RELATIONS,
    DEFAULT_ARTIFACT_MATURITY,
    Artifact,
    ArtifactGoverns,
    ArtifactVersion,
)
from snowline_governance.scope_client import ScopeClient


class ArtifactNotFoundError(Exception):
    """No artifact with the given id."""


class GitBackendUnsupportedError(Exception):
    """The git artifact backend is not available in the governance plugin yet.

    Git-backed artifacts resolve their content from a repo doc, which needs a
    repo registry + a GitHub-side content transport — a GitHub-plugin concern,
    out of scope for this increment (spec §4 carve). Register an inline artifact
    instead (its content lives in the substrate on `body`)."""


# --- governs normalization (carried from the monolith's `_normalize_governs`) --


class _GovernsUnset:
    """Sentinel: the `governs` argument was not supplied at all (distinct from
    `None`, the explicit 'clear' form). Lets register leave governs untouched when
    a caller doesn't pass it."""


_GOVERNS_UNSET = _GovernsUnset()


def _normalize_governs(governs) -> tuple[bool, list[str] | None]:
    """Validate the polymorphic `governs` argument into `(governs_all, slugs)`.

    Accepts a single slug string, a list of slugs, the literal ``"*"`` (all
    scopes), or ``None`` (clear). Returns ``(True, None)`` for ``"*"``,
    ``(False, None)`` for ``None``, ``(False, [slug, ...])`` otherwise. Raises
    ``ValueError`` for a list containing ``"*"`` (mixing forms) or an EMPTY list
    (a likely mistake — `None` is the explicit clear). Does NOT check slug
    existence (the caller resolves slugs against the platform first)."""
    if governs is None:
        return (False, None)
    if isinstance(governs, str):
        if governs == "*":
            return (True, None)
        return (False, [governs])
    if isinstance(governs, (list, tuple)):
        slugs = list(governs)
        if "*" in slugs:
            raise ValueError(
                "governs cannot mix '*' (all scopes) with explicit slugs — "
                "pass either '*' or a list of scope slugs, not both"
            )
        if not slugs:
            raise ValueError(
                "governs=[] is empty — pass None to clear governs, or a "
                "non-empty list of scope slugs"
            )
        return (False, slugs)
    raise ValueError(
        f"governs must be a scope slug, a list of slugs, '*', or None — "
        f"got {governs!r}"
    )


def _apply_governs(
    session: Session,
    a: Artifact,
    governs,
    resolved_scopes: dict[str, dict] | None = None,
) -> None:
    """Set an artifact's governance from the polymorphic `governs` argument (the
    shared write path for register + set_governs). Replaces the association rows
    wholesale and toggles `governs_all`. The two forms are mutually exclusive:
    `*` sets the flag and clears rows; a slug/list clears the flag and sets rows;
    None clears both.

    Scope slugs are SOFT references — the caller (the MCP surface) has already
    resolved each slug against the platform's `ScopeClient` and passes the
    resolution map `resolved_scopes` (slug → the platform's scope row, carrying
    the stable `id`). The governs rows key on that STABLE `scope_id` (#11), with
    the slug denormalized for display."""
    governs_all, slugs = _normalize_governs(governs)
    resolved_scopes = resolved_scopes or {}
    rows: list[tuple[uuid.UUID, str]] = []
    if slugs is not None:
        for slug in slugs:
            sc = resolved_scopes.get(slug)
            if sc is None:
                raise ValueError(
                    f"governs scope {slug!r} not resolved — register it on the "
                    "platform first"
                )
            rows.append((uuid.UUID(str(sc["id"])), sc["slug"]))
    # Replace rows wholesale (delete-then-insert), set the flag.
    session.execute(
        ArtifactGoverns.__table__.delete().where(
            ArtifactGoverns.artifact_id == a.id
        )
    )
    a.governs_all = governs_all
    # dedupe by scope_id while preserving determinism (a list with repeats is a
    # caller slip, not an error — collapse it).
    seen: set[uuid.UUID] = set()
    for sid, sslug in rows:
        if sid in seen:
            continue
        seen.add(sid)
        session.add(
            ArtifactGoverns(artifact_id=a.id, scope_id=sid, scope_slug=sslug)
        )
    session.flush()


# --- leaf / version helpers (carried from the monolith) ---------------------


def _leaf_stmt(artifact_id):
    """**Current** leaf versions of an artifact: structural leaves (nothing
    supersedes them, via the shared DAG helper) that aren't resolved away
    (`resolve_artifact` marks a losing leaf `status=superseded`). Most recent
    first, `id` tiebroken so `current` is deterministic when two leaves share a
    `created_at` (same-txn)."""
    return (
        select(ArtifactVersion)
        .where(
            ArtifactVersion.artifact_id == artifact_id,
            ArtifactVersion.status != "superseded",
            branching.leaf_filter(
                ArtifactVersion.id,
                ArtifactVersion.supersedes_id,
                ArtifactVersion.artifact_id == artifact_id,
            ),
        )
        .order_by(ArtifactVersion.created_at.desc(), ArtifactVersion.id.desc())
    )


def _leaves(session: Session, artifact_id) -> list[ArtifactVersion]:
    """All current leaves. >1 means the artifact has competing branches."""
    return list(session.scalars(_leaf_stmt(artifact_id)))


def _current_version(session: Session, artifact_id) -> ArtifactVersion | None:
    """The single 'current' leaf — the latest one. (When branches compete,
    `leaves` in the dict surfaces all of them; this is the stable default.)"""
    return session.scalars(_leaf_stmt(artifact_id).limit(1)).first()


def _version_dict(v: ArtifactVersion | None) -> dict | None:
    if v is None:
        return None
    return {
        "id": str(v.id),
        "status": v.status,
        "relation": v.relation,
        "has_snapshot": v.body_snapshot is not None,
        "summary": v.summary,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


def _governs(session: Session, artifact_id) -> list[str]:
    """The scope slugs an artifact governs via `artifact_governs` rows, sorted
    (deterministic read shape). Empty when ungoverned or `governs_all` — the
    all-scopes form is the separate boolean, not enumerated rows here."""
    rows = session.scalars(
        select(ArtifactGoverns.scope_slug)
        .where(ArtifactGoverns.artifact_id == artifact_id)
        .order_by(ArtifactGoverns.scope_slug.asc())
    )
    return list(rows)


def _artifact_dict(session: Session, a: Artifact) -> dict:
    count = session.scalar(
        select(func.count())
        .select_from(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == a.id)
    )
    leaves = _leaves(session, a.id)
    leaf_dicts = [_version_dict(v) for v in leaves]
    points = branching.branch_points(
        session,
        ArtifactVersion.supersedes_id,
        ArtifactVersion.artifact_id == a.id,
    )
    return {
        "id": str(a.id),
        "doc_kind": a.doc_kind,
        "backend": a.backend,
        "repo": a.repo,
        "path": a.path,
        "maturity": a.maturity,
        "governs": _governs(session, a.id),
        "governs_all": a.governs_all,
        # current_version is the latest leaf; `leaves` surfaces all competing
        # leaves (len>1 ⇒ branched) with ZERO inference.
        "current_version": leaf_dicts[0] if leaf_dicts else None,
        "leaves": leaf_dicts,
        "is_branched": len(leaves) > 1,
        "branch_points": [str(p) for p in points],
        "version_count": count or 0,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _require_artifact(session: Session, artifact_id) -> Artifact:
    try:
        key = uuid.UUID(str(artifact_id))
    except (ValueError, TypeError):
        raise ArtifactNotFoundError(f"no artifact {artifact_id!r}") from None
    a = session.get(Artifact, key)
    if a is None:
        raise ArtifactNotFoundError(f"no artifact {artifact_id!r}")
    return a


# --- writes -----------------------------------------------------------------


def register_artifact(
    session: Session,
    body: str | None = None,
    doc_kind: str = "spec",
    maturity: str = DEFAULT_ARTIFACT_MATURITY,
    governs=None,
    backend: str = ARTIFACT_BACKEND_INLINE,
    resolved_scopes: dict[str, dict] | None = None,
) -> dict:
    """Register an INLINE governing artifact — content lives in the substrate on
    the initial version's `body_snapshot` (spec §6.3). Creates the `Artifact`
    plus an initial version; the current version is the derived leaf.

    `body` is REQUIRED for an inline artifact — its content is the substrate
    snapshot, not a repo doc. Pass `body=''` for a deliberately-empty doc; `None`
    would mint a content-less artifact that resolves to nothing.

    `backend` only accepts `'inline'` this increment; `'git'` raises
    `GitBackendUnsupportedError` (the git backend needs a repo registry that's a
    GitHub-plugin concern, out of scope here). Each inline register mints a fresh
    artifact (there is no `(repo, path)` identity to be idempotent on).

    `governs` (single slug / list / `'*'` / None) and `resolved_scopes` (slug →
    the platform's scope row) come from the MCP surface, which resolves each slug
    against the platform first; the governs rows key on the STABLE `scope_id`."""
    if backend == ARTIFACT_BACKEND_GIT:
        raise GitBackendUnsupportedError(
            "the git artifact backend is not available in the governance plugin "
            "yet — it needs a repo registry + a GitHub-side content transport "
            "(a GitHub-plugin concern). Register an inline artifact instead: "
            "register_artifact(body=..., backend='inline')."
        )
    if backend not in ARTIFACT_BACKENDS:
        raise ValueError(
            f"backend must be one of {list(ARTIFACT_BACKENDS)}, got {backend!r}"
        )
    if doc_kind not in ARTIFACT_DOC_KINDS:
        raise ValueError(
            f"doc_kind must be one of {list(ARTIFACT_DOC_KINDS)}, got {doc_kind!r}"
        )
    if maturity not in ARTIFACT_MATURITIES:
        raise ValueError(
            f"maturity must be one of {list(ARTIFACT_MATURITIES)}, got {maturity!r}"
        )
    if body is None:
        raise ValueError(
            "an inline artifact needs a body — its content lives in the "
            "substrate, not a repo doc. Pass body='' for a deliberately-empty "
            "doc; None would mint a content-less artifact that resolves to "
            "nothing."
        )
    _normalize_governs(governs)  # validate up front (raises cleanly)
    artifact = Artifact(
        doc_kind=doc_kind, backend=ARTIFACT_BACKEND_INLINE, maturity=maturity,
    )
    session.add(artifact)
    session.flush()
    initial = ArtifactVersion(artifact_id=artifact.id, body_snapshot=body)
    session.add(initial)
    if governs is not None:
        _apply_governs(session, artifact, governs, resolved_scopes)
    session.flush()
    # STREAM emit (replication-continuity §4 coverage, #79): ONE creation write
    # — artifact + initial version + governs — one event, same transaction.
    replication_stream.emit(
        session,
        EVENT_ARTIFACT_REGISTERED,
        replication_stream.artifact_registered_payload(session, artifact, initial),
    )
    return _artifact_dict(session, artifact)


def revise_artifact(
    session: Session,
    artifact_id: str,
    relation: str,
    supersedes: str | None = None,
    body_snapshot: str | None = None,
    summary: str | None = None,
) -> dict:
    """Create a new version of an artifact (spec §4.2). `relation` is `refines`
    (the normal improve) or `pivot` (a redirection — kept as a branch off its
    predecessor for the record). `supersedes` is the version id the new one
    follows; defaults to the artifact's current leaf. Superseding a NON-leaf older
    version creates a competing branch (both leaves then surface in `leaves`, no
    inference). For an inline artifact `body_snapshot` is the new content; `summary`
    is an optional one-line "why this version"."""
    a = _require_artifact(session, artifact_id)
    if relation not in ARTIFACT_RELATIONS:
        raise ValueError(
            f"relation must be one of {list(ARTIFACT_RELATIONS)}, got {relation!r}"
        )
    if supersedes is None:
        cur = _current_version(session, a.id)
        if cur is None:  # register always creates v1, so this is defensive
            raise ValueError("artifact has no version to supersede")
        sup_id = cur.id
    else:
        try:
            sup_key = uuid.UUID(str(supersedes))
        except (ValueError, TypeError):
            raise ValueError(
                f"supersedes is not a version id: {supersedes!r}"
            ) from None
        sup = session.get(ArtifactVersion, sup_key)
        if sup is None or sup.artifact_id != a.id:
            raise ValueError(
                f"version {supersedes!r} is not a version of this artifact"
            )
        sup_id = sup.id
    version = ArtifactVersion(
        artifact_id=a.id,
        supersedes_id=sup_id,
        relation=relation,
        body_snapshot=body_snapshot,
        summary=summary,
    )
    session.add(version)
    session.flush()
    replication_stream.emit(
        session,
        EVENT_ARTIFACT_REVISED,
        replication_stream.artifact_revised_payload(version),
    )
    return _artifact_dict(session, a)


def resolve_artifact(session: Session, artifact_id: str, version_id: str) -> dict:
    """Resolve competing leaves to one canonical version (spec §4.3). `version_id`
    is the LOSING leaf — it flips to `status=superseded` (the status-flip model: a
    node supersedes only one parent, so we can't merge two leaves into one node;
    instead we drop the loser from the *current* leaves while keeping it in the
    audit trail). Requires the artifact to have >1 current leaf and `version_id`
    to be one of them; the OTHER leaf becomes canonical."""
    a = _require_artifact(session, artifact_id)
    leaves = _leaves(session, a.id)
    if len(leaves) < 2:
        raise ValueError(
            "nothing to resolve — the artifact has a single current leaf"
        )
    try:
        vkey = uuid.UUID(str(version_id))
    except (ValueError, TypeError):
        raise ValueError(
            f"version_id is not a version id: {version_id!r}"
        ) from None
    target = next((v for v in leaves if v.id == vkey), None)
    if target is None:
        raise ValueError(
            f"{version_id!r} is not a current competing leaf of this artifact"
        )
    target.status = "superseded"
    session.flush()
    # STREAM emit — a one-way status flip (monotone; apply is idempotent, no
    # LWW register needed: two sides flipping DIFFERENT losers both converge).
    replication_stream.emit(
        session,
        EVENT_ARTIFACT_RESOLVED,
        replication_stream.artifact_resolved_payload(a.id, target.id),
    )
    return _artifact_dict(session, a)


def set_maturity(session: Session, artifact_id: str, maturity: str) -> dict:
    """Set an Artifact's `maturity` (spec §3.1 / §6.3) — draft → exploratory →
    stable. It's a descriptor, not a gate, so any direction is allowed and no
    version is created (maturity is the artifact's standing, distinct from a
    version snapshot's `status`). Returns the refreshed artifact."""
    if maturity not in ARTIFACT_MATURITIES:
        raise ValueError(
            f"maturity must be one of {list(ARTIFACT_MATURITIES)}, got {maturity!r}"
        )
    a = _require_artifact(session, artifact_id)
    a.maturity = maturity
    session.flush()
    # STREAM emit — an in-place LWW register write (§6): the hook records this
    # write's (at, source) coordinate so a partitioned race resolves the same
    # way on both sides.
    replication_stream.emit(
        session,
        EVENT_ARTIFACT_MATURITY_SET,
        replication_stream.maturity_set_payload(a),
    )
    return _artifact_dict(session, a)


def set_governs(
    session: Session,
    artifact_id: str,
    governs,
    resolved_scopes: dict[str, dict] | None = None,
) -> dict:
    """Set (or clear) an Artifact's `governs` after registration (spec §6.3).
    `governs` accepts a single scope slug, a list of slugs, the literal ``"*"``
    (all scopes → `governs_all`, rows cleared), or ``None`` to clear both. The new
    set fully REPLACES the old. A list containing ``"*"`` or an empty list raises;
    an unresolved slug raises (the MCP surface resolves slugs against the platform
    first and passes `resolved_scopes`). The governs rows key on the STABLE
    `scope_id` (#11). Returns the refreshed artifact."""
    a = _require_artifact(session, artifact_id)
    _apply_governs(session, a, governs, resolved_scopes)
    # STREAM emit — the governs SET is one LWW register (the write is a
    # wholesale replace, so the whole set is the contested value).
    replication_stream.emit(
        session,
        EVENT_ARTIFACT_GOVERNS_SET,
        replication_stream.governs_set_payload(session, a),
    )
    return _artifact_dict(session, a)


# --- reads ------------------------------------------------------------------


def get_artifact(session: Session, artifact_id: str) -> dict:
    """The full artifact — identity, lifecycle, governs, the current leaf + all
    competing leaves + branch points — by id. Read-only; raises on an
    unknown/invalid id (parsed first, so a non-UUID is a clean not-found)."""
    return _artifact_dict(session, _require_artifact(session, artifact_id))


def _resolve_list_limit(limit: int | None, default: int = 50, cap: int = 500) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), cap))


def _governs_for_artifacts(
    session: Session, artifact_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """`_governs` for a SET of artifacts in ONE query (issue #14): artifact_id →
    its governed scope slugs, sorted (the same deterministic per-artifact shape
    `_governs` returns). Missing/ungoverned artifacts map to an empty list."""
    out: dict[uuid.UUID, list[str]] = {aid: [] for aid in artifact_ids}
    if not artifact_ids:
        return out
    for art_id, slug in session.execute(
        select(ArtifactGoverns.artifact_id, ArtifactGoverns.scope_slug)
        .where(ArtifactGoverns.artifact_id.in_(artifact_ids))
        .order_by(ArtifactGoverns.scope_slug.asc())
    ):
        out.setdefault(art_id, []).append(slug)
    return out


def _version_counts_for_artifacts(
    session: Session, artifact_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Total version count per artifact for a SET of artifacts in ONE grouped
    query (issue #14). Missing artifacts map to 0."""
    out: dict[uuid.UUID, int] = {aid: 0 for aid in artifact_ids}
    if not artifact_ids:
        return out
    for art_id, n in session.execute(
        select(ArtifactVersion.artifact_id, func.count())
        .where(ArtifactVersion.artifact_id.in_(artifact_ids))
        .group_by(ArtifactVersion.artifact_id)
    ):
        out[art_id] = n
    return out


def _leaf_counts_for_artifacts(
    session: Session, artifact_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Current-leaf count per artifact for a SET of artifacts in ONE grouped query
    (issue #14) — the `is_branched` signal (leaf_count > 1). A leaf is a
    non-`superseded` structural leaf; supersession is intra-artifact, so the leaf
    filter's sub-select scopes safely to the whole id set (`artifact_id IN (:ids)`)
    and resolves exactly as the per-artifact filter does. Missing artifacts → 0."""
    out: dict[uuid.UUID, int] = {aid: 0 for aid in artifact_ids}
    if not artifact_ids:
        return out
    for art_id, n in session.execute(
        select(ArtifactVersion.artifact_id, func.count())
        .where(
            ArtifactVersion.artifact_id.in_(artifact_ids),
            ArtifactVersion.status != "superseded",
            branching.leaf_filter(
                ArtifactVersion.id,
                ArtifactVersion.supersedes_id,
                ArtifactVersion.artifact_id.in_(artifact_ids),
            ),
        )
        .group_by(ArtifactVersion.artifact_id)
    ):
        out[art_id] = n
    return out


def _compact_row_from_signals(
    a: Artifact,
    *,
    version_count: int,
    leaf_count: int,
    governs: list[str],
) -> dict:
    """Build the `_artifact_compact_row` shape from PRE-FETCHED signals — the
    batched read path's per-artifact assembler (no DB queries). Identical shape to
    `_artifact_compact_row`."""
    return {
        "id": str(a.id),
        "doc_kind": a.doc_kind,
        "backend": a.backend,
        "repo": a.repo,
        "path": a.path,
        "maturity": a.maturity,
        "governs": governs,
        "governs_all": a.governs_all,
        "version_count": version_count or 0,
        "is_branched": (leaf_count or 0) > 1,
    }


def _artifact_compact_row(session: Session, a: Artifact) -> dict:
    """A small artifact header for `list_artifacts`: identity (id + `repo`/`path`,
    the human-readable identity a reader needs to tell one governing doc from
    another) + lifecycle + the two cheap derived signals a sweep needs
    (`version_count`, `is_branched`), WITHOUT the full leaves / current_version /
    branch_points. Expand any row via `get_artifact(id)`."""
    version_count = session.scalar(
        select(func.count())
        .select_from(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == a.id)
    )
    leaf_count = session.scalar(
        select(func.count())
        .select_from(ArtifactVersion)
        .where(
            ArtifactVersion.artifact_id == a.id,
            ArtifactVersion.status != "superseded",
            branching.leaf_filter(
                ArtifactVersion.id,
                ArtifactVersion.supersedes_id,
                ArtifactVersion.artifact_id == a.id,
            ),
        )
    )
    return {
        "id": str(a.id),
        "doc_kind": a.doc_kind,
        "backend": a.backend,
        "repo": a.repo,
        "path": a.path,
        "maturity": a.maturity,
        "governs": _governs(session, a.id),
        "governs_all": a.governs_all,
        "version_count": version_count or 0,
        "is_branched": (leaf_count or 0) > 1,
    }


def list_artifacts(
    session: Session,
    governs: str | None = None,
    governs_scope_id: uuid.UUID | str | None = None,
    limit: int | None = None,
) -> dict:
    """List registered artifacts as compact rows. Newest first, capped at `limit`
    (default 50, max 500) with `items_total` carrying the true depth. Expand any
    row via `get_artifact(id)`. Read-only.

    `governs` (a scope slug, with its resolved `governs_scope_id` from the MCP
    surface) narrows to artifacts governing that scope — matched by the STABLE
    `scope_id` via an association row OR the all-scopes `governs_all` flag (a
    `governs_all` artifact governs every scope, so it surfaces under a per-scope
    filter too). An unresolved/None scope id with a `governs` slug yields an empty
    list (not an error)."""
    lim = _resolve_list_limit(limit)
    stmt = select(Artifact).order_by(
        Artifact.created_at.desc(), Artifact.id.desc()
    )
    if governs is not None:
        if governs_scope_id is None:
            return {"governs": governs, "artifacts": [], "items_total": 0}
        sid = (
            governs_scope_id
            if isinstance(governs_scope_id, uuid.UUID)
            else uuid.UUID(str(governs_scope_id))
        )
        governing_ids = select(ArtifactGoverns.artifact_id).where(
            ArtifactGoverns.scope_id == sid
        )
        stmt = stmt.where(
            or_(Artifact.id.in_(governing_ids), Artifact.governs_all.is_(True))
        )
    rows = list(session.scalars(stmt))
    return {
        "governs": governs,
        "artifacts": [_artifact_compact_row(session, a) for a in rows[:lim]],
        "items_total": len(rows),
    }


def applicable_artifacts(
    session: Session,
    scope_slug: str,
    scope_client: ScopeClient,
    *,
    limit: int = 50,
) -> dict:
    """Artifacts APPLICABLE at a scope, ANCESTOR-INHERITED — the §6.1 isolation-
    aware walk applied to artifact `governs`-matching (spec §6.3: "the same
    isolation-aware walk governs artifact governs-matching").

    Mirrors `decisions.applicable_decisions`: it asks the injected `scope_client`
    for the reader's ancestor chain (`GET /scopes/{slug}/ancestors`) — own scope
    first, then each `parent_id` ancestor nearest-first, the platform HALTING at
    the first `isolated` node + the forest root. An artifact applies when it has
    a governs edge to ANY chain scope (matched by the STABLE `scope_id` (#11), not
    the mutable slug) OR `governs_all` is set. Each inherited row carries
    `from_scope` (the ancestor slug it matched); the reader's OWN-scope matches
    omit it. `governs_all` artifacts carry `from_scope='*'` (they apply
    everywhere, not from a particular ancestor).

    PRECEDENCE UNDER THE CAP: own/ancestor edge matches (most specific) come
    first, then `governs_all` (catch-all); `[:limit]` is applied last, so when a
    scope has more edge matches than `limit` the broad `governs_all` docs are the
    ones truncated. `items_total` carries the true pre-cap count, so a caller
    seeing `items_total > len(artifacts)` knows to raise `limit`."""
    chain = scope_client.ancestors(scope_slug)
    chain_ids = [uuid.UUID(str(sc["id"])) for sc in chain]
    # depth → slug for the from_scope tag (own scope = depth 0).
    depth_slug = {i: sc["slug"] for i, sc in enumerate(chain)}
    id_depth = {sid: i for i, sid in enumerate(chain_ids)}

    seen: set[uuid.UUID] = set()
    # (artifact_id, from_scope_tag) in final order: edge matches nearest-first,
    # then governs_all (catch-all). The from_scope tag is None for own-scope
    # edge matches, the ancestor slug for inherited edges, '*' for governs_all.
    ordered: list[tuple[uuid.UUID, str | None]] = []

    if chain_ids:
        # ONE query: artifacts with a governs edge to any chain scope.
        edge_rows = session.execute(
            select(ArtifactGoverns.artifact_id, ArtifactGoverns.scope_id)
            .where(ArtifactGoverns.scope_id.in_(chain_ids))
        ).all()
        # Pick the NEAREST chain scope each artifact matches (smallest depth).
        nearest: dict[uuid.UUID, int] = {}
        for art_id, sid in edge_rows:
            d = id_depth[sid]
            if art_id not in nearest or d < nearest[art_id]:
                nearest[art_id] = d
        for art_id, depth in sorted(nearest.items(), key=lambda kv: kv[1]):
            seen.add(art_id)
            ordered.append((art_id, None if depth == 0 else depth_slug[depth]))

    # ONE query: `governs_all` artifacts apply everywhere — tagged from_scope='*'.
    for art_id in session.scalars(
        select(Artifact.id)
        .where(Artifact.governs_all.is_(True))
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
    ):
        if art_id in seen:
            continue
        seen.add(art_id)
        ordered.append((art_id, "*"))

    # Batch-load the Artifact rows + the three compact-row signals for the WHOLE
    # matched set in a bounded number of queries (issue #14: was ~3 queries +
    # a session.get PER matched artifact). Assemble rows from the pre-fetched
    # signals — no per-item DB round-trips.
    art_ids = [aid for aid, _ in ordered]
    arts: dict[uuid.UUID, Artifact] = {
        a.id: a
        for a in session.scalars(
            select(Artifact).where(Artifact.id.in_(art_ids))
        )
    } if art_ids else {}
    version_counts = _version_counts_for_artifacts(session, art_ids)
    leaf_counts = _leaf_counts_for_artifacts(session, art_ids)
    governs_map = _governs_for_artifacts(session, art_ids)

    collected: list[dict] = []
    for art_id, from_scope in ordered:
        a = arts.get(art_id)
        if a is None:  # edge/flag row whose artifact vanished — skip defensively
            continue
        row = _compact_row_from_signals(
            a,
            version_count=version_counts.get(art_id, 0),
            leaf_count=leaf_counts.get(art_id, 0),
            governs=governs_map.get(art_id, []),
        )
        if from_scope is not None:
            row["from_scope"] = from_scope
        collected.append(row)

    total = len(collected)
    return {
        "scope": scope_slug,
        "artifacts": collected[:limit],
        "items_total": total,
    }
