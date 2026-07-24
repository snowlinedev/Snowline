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

import re
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
from snowline_governance.milestone_client import MilestoneClient
from snowline_governance.scope_client import ScopeClient


# --- milestone slug grammar (spec §4; the #141 clientless fallback) -----------
#
# Since #145 the `ArtifactVersion.milestone` stamp is a RESOLUTION KEY validated
# against the platform registry at mint (see `_stamp_milestone` and
# milestones.md §6.1). This grammar validator is retained for the CLIENTLESS
# fallback only — a direct service call with no `MilestoneClient` injected keeps
# the prior #141 posture (grammar-checked, stored verbatim). The grammar is
# CARRIED (not imported — import-purity across the service boundary) from
# `snowline_platform.scopes` §2.1, and input is folded to the canonical
# lowercase form per #139.
_MILESTONE_SEG = r"[._-]*[a-z0-9][a-z0-9._-]*"
_MILESTONE_RE = re.compile(rf"^{_MILESTONE_SEG}(/{_MILESTONE_SEG})*$")


def _canonical_milestone(milestone):
    """Fold a milestone slug to the canonical lowercase form (#139). ASCII-ONLY:
    non-ASCII input passes through untouched so the grammar check rejects it
    LOUDLY — a Unicode case-fold (U+212A → 'k') must not smuggle an invalid input
    in as a silently different slug. Non-strings pass through."""
    if isinstance(milestone, str) and milestone.isascii():
        return milestone.strip().lower()
    return milestone


def _validate_milestone(milestone: str | None) -> str | None:
    """Validate an OPTIONAL milestone slug against the platform slug grammar and
    return the CANONICAL (lowercased) form (#139). A soft ref, never resolved —
    so this is grammar-only, like a scope-slug validation minus the platform
    round-trip. `None`/empty pass through as None (an unstamped version); garbage
    is rejected LOUDLY (spec §4 — the milestone names no scope, so a typo can't
    self-heal via resolution)."""
    if milestone is None:
        return None
    milestone = _canonical_milestone(milestone)
    if isinstance(milestone, str):
        milestone = milestone.strip()
    if not milestone:
        return None
    if not isinstance(milestone, str) or not _MILESTONE_RE.match(milestone):
        raise ValueError(
            f"invalid milestone slug {milestone!r} — expected a platform-style "
            "slug (lowercase `name` or `org/rest`, the §2.1 grammar)"
        )
    return milestone


# --- first-class milestone consumer (milestones.md §6.1) ---------------------
#
# Since #145 the stamp is a RESOLUTION KEY, not a soft slug: the write path
# resolves it against the platform milestone registry and stores the CANONICAL
# address, and version canonicality becomes a function of milestone STATE read
# from the platform (§6.1). The `MilestoneClient` is injected (the MCP surface
# always wires the real/stub client); a clientless direct call degrades to the
# prior #141 grammar-only verbatim posture so the service stays callable without
# a platform (the wired surfaces never take that path).

# Statuses that make a stamped version ELIGIBLE for canonicality (§6.1.2). An
# ABSENT stamp is eligible too; `planned` is pending; `cancelled` is dead.
_ELIGIBLE_STATUSES = frozenset({"active", "achieved"})
# Terminal statuses a mint may not stamp with (§6.1.1) absent an override.
_TERMINAL_STATUSES = frozenset({"achieved", "cancelled"})

_BUCKET_ELIGIBLE = "eligible"
_BUCKET_PENDING = "pending"
_BUCKET_DEAD = "dead"
_BUCKET_LEGACY = "legacy"
# Buckets that COUNT toward canonicality: eligible, plus legacy (an unresolvable
# stamp is treated as ABSENT for canonicality — annotation-only — §6.1.2).
_CANONICALITY_BUCKETS = frozenset({_BUCKET_ELIGIBLE, _BUCKET_LEGACY})


def _stamp_milestone(
    ref: str | None,
    milestone_client: MilestoneClient | None,
    *,
    context: str | None,
    allow_terminal: bool,
) -> str | None:
    """Resolve a milestone REF to store at mint (§6.1.1). Returns the CANONICAL
    address (or None for an empty/absent stamp).

    With no `milestone_client` this degrades to the #141 grammar-only verbatim
    posture (the wired surfaces always inject one). With a client:
      - a BARE ref (no `/`) requires `context` — the artifact's single governing
        scope; the caller passes `context=None` when the artifact governs a
        list, all scopes, or nothing, and a bare ref is then REJECTED (full
        address required);
      - resolution is delegated to the platform (unknown → the client raises
        `MilestoneResolutionError` carrying the platform's suggestions);
      - an `achieved`/`cancelled` milestone is rejected unless `allow_terminal`
        — a post-hoc terminal stamp rewrites what a released version reports as
        shipped.
    """
    if milestone_client is None:
        return _validate_milestone(ref)
    if ref is None:
        return None
    if isinstance(ref, str):
        ref = ref.strip()
    if isinstance(ref, str) and not ref:
        return None
    if not isinstance(ref, str):
        raise ValueError(
            f"invalid milestone ref {ref!r} — expected a milestone name or "
            "address string"
        )
    if "/" not in ref and context is None:
        raise ValueError(
            f"milestone {ref!r} is a bare name but this artifact has no single "
            "governing scope to resolve it against (it governs a list of "
            "scopes, all scopes, or nothing) — pass the full milestone address "
            "(anchor/name)."
        )
    row = milestone_client.resolve(ref, context=context)
    address = row.get("address")
    status = row.get("status")
    if status in _TERMINAL_STATUSES and not allow_terminal:
        raise ValueError(
            f"milestone {address!r} is {status} — stamping a version with a "
            "terminal (achieved/cancelled) milestone would rewrite what "
            "list_artifact_versions reports a release shipped as. Pass "
            "allow_terminal_milestone=True to stamp it deliberately (§6.1.1)."
        )
    return address


def _bucket_stamps(
    stamps, milestone_client: MilestoneClient
) -> dict[str, tuple[str, bool]]:
    """Resolve a SET of distinct milestone stamps in ONE resolve_batch call and
    map each to `(bucket, unresolved)` (§6.1.2). A stamp that doesn't resolve
    (per-ref `{error}` in the batch) buckets as `legacy` — treated as ABSENT for
    canonicality, flagged for backfill. A transport failure propagates as
    `MilestoneServiceError` (a HARD read error — an unreadable stamp is NEVER
    treated as absent)."""
    stamps = sorted({s for s in stamps if s})
    if not stamps:
        return {}
    results = milestone_client.resolve_batch(stamps)
    out: dict[str, tuple[str, bool]] = {}
    for s in stamps:
        r = results.get(s)
        if not r or "error" in r:
            out[s] = (_BUCKET_LEGACY, True)
            continue
        status = r.get("status")
        if status in _ELIGIBLE_STATUSES:
            out[s] = (_BUCKET_ELIGIBLE, False)
        elif status == "planned":
            out[s] = (_BUCKET_PENDING, False)
        elif status == "cancelled":
            out[s] = (_BUCKET_DEAD, False)
        else:  # unknown status → treat as absent/annotation-only, flagged
            out[s] = (_BUCKET_LEGACY, True)
    return out


def _version_bucket(
    v: "ArtifactVersion", stamp_buckets: dict[str, tuple[str, bool]]
) -> tuple[str, bool]:
    """This version's `(bucket, unresolved)`. An unstamped version is eligible
    (absent stamp); a stamped one takes its milestone's bucket (legacy if it was
    not in the resolved set)."""
    if not v.milestone:
        return (_BUCKET_ELIGIBLE, False)
    return stamp_buckets.get(v.milestone, (_BUCKET_LEGACY, True))


def _live_versions(session: Session, artifact_id) -> list["ArtifactVersion"]:
    """All non-`superseded` versions of an artifact — the graph canonicality is
    computed over (a resolve_artifact loser stays out, as today)."""
    return list(
        session.scalars(
            select(ArtifactVersion).where(
                ArtifactVersion.artifact_id == artifact_id,
                ArtifactVersion.status != "superseded",
            )
        )
    )


def _child_map(versions: list["ArtifactVersion"]) -> dict:
    """`parent version id -> [child version ids]` over the given version set —
    the supersession DAG edges (child.supersedes_id points at its parent)."""
    ids = {v.id for v in versions}
    children: dict = {}
    for v in versions:
        if v.supersedes_id in ids:
            children.setdefault(v.supersedes_id, []).append(v.id)
    return children


def _induced_leaves(
    versions: list["ArtifactVersion"], member_ids: set
) -> list["ArtifactVersion"]:
    """Leaves of the DAG induced on `member_ids`: a member with NO other member
    reachable as a descendant (following supersession edges through ANY
    intermediate — a pending/dead version between two eligible ones does not
    fork the line, it transmits it, §6.1.3). Newest-first, id-tiebroken, so the
    default pick is deterministic; `len > 1` is genuine competition."""
    children = _child_map(versions)
    leaves: list["ArtifactVersion"] = []
    for v in versions:
        if v.id not in member_ids:
            continue
        stack = list(children.get(v.id, []))
        seen: set = set()
        has_member_desc = False
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            if c in member_ids:
                has_member_desc = True
                break
            stack.extend(children.get(c, []))
        if not has_member_desc:
            leaves.append(v)
    leaves.sort(key=lambda x: (x.created_at, x.id), reverse=True)
    return leaves


def _structural_leaves(
    versions: list["ArtifactVersion"],
) -> list["ArtifactVersion"]:
    """Structural leaves over a live version set (nothing live supersedes them) —
    the milestone-agnostic `is_branched` signal, newest-first."""
    children = _child_map(versions)
    leaves = [v for v in versions if not children.get(v.id)]
    leaves.sort(key=lambda x: (x.created_at, x.id), reverse=True)
    return leaves


def _eligible_leaves(
    session: Session,
    artifact_id,
    milestone_client: MilestoneClient,
) -> tuple[list["ArtifactVersion"], dict[str, tuple[str, bool]], list["ArtifactVersion"]]:
    """The CANONICAL competition: `(eligible_leaves, stamp_buckets, live_versions)`
    — the leaves of the eligible subgraph (§6.1.3). ONE resolve_batch call;
    transport failure propagates."""
    versions = _live_versions(session, artifact_id)
    stamp_buckets = _bucket_stamps((v.milestone for v in versions), milestone_client)
    eligible_ids = {
        v.id
        for v in versions
        if _version_bucket(v, stamp_buckets)[0] in _CANONICALITY_BUCKETS
    }
    return _induced_leaves(versions, eligible_ids), stamp_buckets, versions


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


def _version_dict(
    v: ArtifactVersion | None,
    include_body: bool = False,
    bucket: tuple[str, bool] | None = None,
) -> dict | None:
    if v is None:
        return None
    out = {
        "id": str(v.id),
        "status": v.status,
        "relation": v.relation,
        "supersedes_id": str(v.supersedes_id) if v.supersedes_id else None,
        "has_snapshot": v.body_snapshot is not None,
        "summary": v.summary,
        # The milestone stamp (spec §4 / §6.1). Since #145 this is the CANONICAL
        # address the write path resolved + stored; None on an unstamped version.
        "milestone": v.milestone,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }
    # Milestone STATE bucket (§6.1.2), present only on a milestone-aware read
    # (when a `MilestoneClient` was injected). `milestone_unresolved` flags a
    # legacy stamp — one that doesn't resolve — treated as absent for
    # canonicality but surfaced for agent-driven backfill.
    if bucket is not None:
        out["milestone_bucket"] = bucket[0]
        out["milestone_unresolved"] = bucket[1]
    if include_body:
        out["body_snapshot"] = v.body_snapshot
    return out


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


def _artifact_dict(
    session: Session,
    a: Artifact,
    include_body: bool = False,
    milestone_client: MilestoneClient | None = None,
    milestone_filter: set[str] | None = None,
) -> dict:
    """Assemble the artifact read shape.

    Milestone-agnostic (no `milestone_client`): the historical structural view —
    `current_version` is the newest structural leaf. This is the clientless
    fallback the direct-call ergonomics keep.

    Milestone-aware (a `MilestoneClient` injected, always in the wired surfaces):
    canonicality follows milestone STATE (§6.1). `current_version` is the leaf of
    the ELIGIBLE subgraph (or, when `milestone_filter` is given — the
    per-milestone read — the leaf of the subgraph stamped with that milestone,
    §6.1.5). `leaves` are the structural leaves, each annotated with its milestone
    bucket; `competing_leaves` explicitly surfaces >1 competing canonical leaves
    (never a silent tie-break, §6.1.3)."""
    count = session.scalar(
        select(func.count())
        .select_from(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == a.id)
    )
    points = branching.branch_points(
        session,
        ArtifactVersion.supersedes_id,
        ArtifactVersion.artifact_id == a.id,
    )
    base = {
        "id": str(a.id),
        "doc_kind": a.doc_kind,
        "backend": a.backend,
        "repo": a.repo,
        "path": a.path,
        "maturity": a.maturity,
        "governs": _governs(session, a.id),
        "governs_all": a.governs_all,
        "branch_points": [str(p) for p in points],
        "version_count": count or 0,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }

    if milestone_client is None:
        # --- historical structural view (clientless fallback) ----------------
        leaves = _leaves(session, a.id)
        leaf_dicts = [_version_dict(v) for v in leaves]
        if include_body and leaves:
            current = _version_dict(leaves[0], include_body=True)
        else:
            current = leaf_dicts[0] if leaf_dicts else None
        base.update(
            current_version=current,
            leaves=leaf_dicts,
            is_branched=len(leaves) > 1,
        )
        return base

    # --- milestone-aware view (§6.1) -----------------------------------------
    versions = _live_versions(session, a.id)
    stamp_buckets = _bucket_stamps((v.milestone for v in versions), milestone_client)

    def _bkt(v):
        return _version_bucket(v, stamp_buckets)

    # `leaves` are the STRUCTURAL current leaves (milestone-agnostic), each
    # annotated with its bucket — a reader sees every current line and its state.
    structural = _structural_leaves(versions)
    leaf_dicts = [_version_dict(v, bucket=_bkt(v)) for v in structural]

    if milestone_filter is not None:
        # Per-milestone read (§6.1.5): the leaf of the subgraph stamped with the
        # target (alias-set matched). No stamped version → the canonical version.
        member_ids = {v.id for v in versions if v.milestone in milestone_filter}
        canonical_leaves = _induced_leaves(versions, member_ids)
        if not canonical_leaves:
            eligible_ids = {
                v.id for v in versions if _bkt(v)[0] in _CANONICALITY_BUCKETS
            }
            canonical_leaves = _induced_leaves(versions, eligible_ids)
    else:
        # Default read: the leaf of the ELIGIBLE subgraph (§6.1.3).
        eligible_ids = {
            v.id for v in versions if _bkt(v)[0] in _CANONICALITY_BUCKETS
        }
        canonical_leaves = _induced_leaves(versions, eligible_ids)

    current = None
    if canonical_leaves:
        top = canonical_leaves[0]
        current = _version_dict(top, include_body=include_body, bucket=_bkt(top))
    competing = [
        _version_dict(v, bucket=_bkt(v)) for v in canonical_leaves[1:]
    ] if len(canonical_leaves) > 1 else []

    base["current_version"] = current
    base["leaves"] = leaf_dicts
    base["is_branched"] = len(structural) > 1
    # >1 canonical leaves is a GENUINE competition — surfaced, never picked
    # silently (§6.1.3). The default `current_version` is the newest; the rest
    # ride here. Empty when there's a single canonical leaf.
    base["competing_leaves"] = competing
    return base


def _require_artifact(session: Session, artifact_id) -> Artifact:
    try:
        key = uuid.UUID(str(artifact_id))
    except (ValueError, TypeError):
        raise ArtifactNotFoundError(f"no artifact {artifact_id!r}") from None
    a = session.get(Artifact, key)
    if a is None:
        raise ArtifactNotFoundError(f"no artifact {artifact_id!r}")
    return a


def _require_version(
    session: Session, a: Artifact, version_id, param: str = "version_id"
) -> ArtifactVersion:
    """Parse + resolve a version id and require it to belong to artifact `a` —
    the one validation chokepoint for every verb taking a (artifact, version)
    pair. Distinguishes an id that matches NO version anywhere (a not-found)
    from one that exists on a DIFFERENT artifact (a pairing error), so a caller
    holding a stale id isn't sent hunting other artifacts. `param` names the
    caller's parameter in the parse error (e.g. `supersedes`)."""
    try:
        vkey = uuid.UUID(str(version_id))
    except (ValueError, TypeError):
        raise ValueError(f"{param} is not a version id: {version_id!r}") from None
    v = session.get(ArtifactVersion, vkey)
    if v is None:
        raise ValueError(f"no version {version_id!r}")
    if v.artifact_id != a.id:
        raise ValueError(
            f"version {version_id!r} is not a version of this artifact"
        )
    return v


# --- writes -----------------------------------------------------------------


def register_artifact(
    session: Session,
    body: str | None = None,
    doc_kind: str = "spec",
    maturity: str = DEFAULT_ARTIFACT_MATURITY,
    governs=None,
    backend: str = ARTIFACT_BACKEND_INLINE,
    milestone: str | None = None,
    resolved_scopes: dict[str, dict] | None = None,
    milestone_client: MilestoneClient | None = None,
    allow_terminal_milestone: bool = False,
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

    `milestone` is an optional release milestone REF stamped on the initial
    version (milestones.md §6.1.1). With a `milestone_client` injected (the wired
    surface always does) it is RESOLVED against the platform registry and the
    CANONICAL address is stored; unknown refs hard-fail with the platform's
    suggestions, a bare ref needs the artifact's single governing scope as
    context, and an `achieved`/`cancelled` milestone is rejected unless
    `allow_terminal_milestone=True`. Clientless, it degrades to the #141
    grammar-only verbatim posture. `None`/empty leaves the version unstamped.

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
    governs_all, gov_slugs = _normalize_governs(governs)  # validate up front
    # Bare-name milestone context (§6.1.1): the artifact's SINGLE governing scope,
    # normalized per §3. An artifact governing a list, all scopes (`governs_all`),
    # or nothing has NO bare-name context — a bare stamp is then rejected at mint
    # (full address required), enforced inside `_stamp_milestone`.
    context = (
        gov_slugs[0]
        if (not governs_all and gov_slugs is not None and len(gov_slugs) == 1)
        else None
    )
    milestone = _stamp_milestone(
        milestone, milestone_client,
        context=context, allow_terminal=allow_terminal_milestone,
    )
    artifact = Artifact(
        doc_kind=doc_kind, backend=ARTIFACT_BACKEND_INLINE, maturity=maturity,
    )
    session.add(artifact)
    session.flush()
    initial = ArtifactVersion(
        artifact_id=artifact.id, body_snapshot=body, milestone=milestone
    )
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
    return _artifact_dict(session, artifact, milestone_client=milestone_client)


def revise_artifact(
    session: Session,
    artifact_id: str,
    relation: str,
    supersedes: str | None = None,
    body_snapshot: str | None = None,
    summary: str | None = None,
    milestone: str | None = None,
    milestone_client: MilestoneClient | None = None,
    allow_terminal_milestone: bool = False,
) -> dict:
    """Create a new version of an artifact (spec §4.2). `relation` is `refines`
    (the normal improve) or `pivot` (a redirection — kept as a branch off its
    predecessor for the record). For an inline artifact `body_snapshot` is the new
    content; `summary` is an optional one-line "why this version".

    `supersedes` is the version id the new one follows. Since #145 its default is
    the current CANONICAL version — the leaf of the ELIGIBLE subgraph (§6.1.4),
    NOT the DAG leaf (which may be a pending draft). Superseding a pending/dead
    version explicitly is legal, but such a revision MUST carry an explicit
    milestone stamp (inherit-or-state) — an unstamped child of a non-eligible
    parent is rejected, because it would be eligible-absent and instantly
    canonical. (Clientless, the default falls back to the structural current
    leaf and no bucket check runs — the #141 posture.)

    `milestone` is an optional release milestone REF stamped on THIS version
    (per-version, not inherited). With a `milestone_client` it is resolved to its
    CANONICAL address; a bare ref resolves against the artifact's single governing
    scope; a terminal milestone needs `allow_terminal_milestone=True`."""
    a = _require_artifact(session, artifact_id)
    if relation not in ARTIFACT_RELATIONS:
        raise ValueError(
            f"relation must be one of {list(ARTIFACT_RELATIONS)}, got {relation!r}"
        )
    # Bare-name milestone context = the artifact's SINGLE governing scope (§6.1.1).
    gov_slugs = _governs(session, a.id)
    context = (
        gov_slugs[0]
        if (not a.governs_all and len(gov_slugs) == 1)
        else None
    )
    milestone = _stamp_milestone(
        milestone, milestone_client,
        context=context, allow_terminal=allow_terminal_milestone,
    )

    if milestone_client is None:
        # #141 structural posture (clientless direct call).
        if supersedes is None:
            cur = _current_version(session, a.id)
            if cur is None:  # register always creates v1, so this is defensive
                raise ValueError("artifact has no version to supersede")
            sup_id = cur.id
        else:
            sup_id = _require_version(
                session, a, supersedes, param="supersedes"
            ).id
    else:
        # --- milestone-aware write defaults (§6.1.4) -------------------------
        versions = _live_versions(session, a.id)
        stamp_buckets = _bucket_stamps(
            (v.milestone for v in versions), milestone_client
        )
        by_id = {v.id: v for v in versions}
        if supersedes is None:
            # Default to the current CANONICAL version (eligible leaf), NOT the
            # structural leaf. If nothing is eligible yet (e.g. the only version
            # is a pending draft), fall back to the structural current leaf — the
            # non-eligible-parent stamp rule below then forces an explicit stamp.
            eligible_ids = {
                vid
                for vid, v in by_id.items()
                if _version_bucket(v, stamp_buckets)[0] in _CANONICALITY_BUCKETS
            }
            canon = _induced_leaves(versions, eligible_ids)
            if canon:
                target = canon[0]
            else:
                structural = _structural_leaves(versions)
                if not structural:  # defensive — register always creates v1
                    raise ValueError("artifact has no version to supersede")
                target = structural[0]
        else:
            target = _require_version(session, a, supersedes, param="supersedes")
        # A revision whose supersedes-target is pending/dead MUST carry an
        # explicit stamp — otherwise the unstamped child is eligible-absent and
        # instantly canonical, leaking unreleased/retracted content (§6.1.4).
        target_bucket = _version_bucket(target, stamp_buckets)[0]
        if target_bucket in (_BUCKET_PENDING, _BUCKET_DEAD) and milestone is None:
            raise ValueError(
                f"cannot revise off a {target_bucket} version "
                f"({target.milestone!r}) without an explicit milestone stamp — "
                "an unstamped child of a non-eligible parent would be "
                "eligible-absent and instantly canonical, leaking unreleased or "
                "retracted content (§6.1.4). Pass a milestone to state the "
                "revision's release line."
            )
        sup_id = target.id

    version = ArtifactVersion(
        artifact_id=a.id,
        supersedes_id=sup_id,
        relation=relation,
        body_snapshot=body_snapshot,
        summary=summary,
        milestone=milestone,
    )
    session.add(version)
    session.flush()
    replication_stream.emit(
        session,
        EVENT_ARTIFACT_REVISED,
        replication_stream.artifact_revised_payload(version),
    )
    return _artifact_dict(session, a, milestone_client=milestone_client)


def resolve_artifact(
    session: Session,
    artifact_id: str,
    version_id: str,
    milestone_client: MilestoneClient | None = None,
) -> dict:
    """Resolve competing leaves to one canonical version (spec §4.3). `version_id`
    is the LOSING leaf — it flips to `status=superseded` (the status-flip model: a
    node supersedes only one parent, so we can't merge two leaves into one node;
    instead we drop the loser from the *current* leaves while keeping it in the
    audit trail). Requires the artifact to have >1 current leaf and `version_id`
    to be one of them; the OTHER leaf becomes canonical.

    Since #145 the ">1 leaves" precondition means >1 ELIGIBLE leaves (§6.1.3):
    `resolve_artifact` collapses genuine competition WITHIN the eligible bucket,
    so a pending/dead draft is not a resolvable competitor. The status flip stays
    monotone/idempotent for replication convergence. Clientless, it falls back to
    structural leaves (the #141 posture)."""
    a = _require_artifact(session, artifact_id)
    if milestone_client is None:
        leaves = _leaves(session, a.id)
    else:
        leaves, _buckets, _versions = _eligible_leaves(
            session, a.id, milestone_client
        )
    if len(leaves) < 2:
        raise ValueError(
            "nothing to resolve — the artifact has a single current "
            f"{'eligible ' if milestone_client is not None else ''}leaf"
        )
    v = _require_version(session, a, version_id)
    target = next((leaf for leaf in leaves if leaf.id == v.id), None)
    if target is None:
        raise ValueError(
            f"{version_id!r} is not a current competing "
            f"{'eligible ' if milestone_client is not None else ''}leaf of this "
            "artifact"
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
    return _artifact_dict(session, a, milestone_client=milestone_client)


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


def _milestone_alias_set(
    ref: str,
    milestone_client: MilestoneClient,
    *,
    context: str | None,
) -> tuple[str, set[str]]:
    """Resolve a milestone REF once and return `(canonical_address, match_set)`
    where the match set is `{canonical} ∪ aliases` (§5) — the milestone-keyed
    reads match stored stamps against the target's FULL alias set, so a stamp
    stored under a since-merged slug still matches. A bare ref needs `context`;
    unknown → the client's `MilestoneResolutionError` (with suggestions)
    propagates."""
    if "/" not in ref and context is None:
        raise ValueError(
            f"milestone {ref!r} is a bare name with no resolution context (the "
            "artifact has no single governing scope, or this is a "
            "portfolio-wide read) — pass the full milestone address "
            "(anchor/name)."
        )
    row = milestone_client.resolve(ref, context=context)
    address = row.get("address")
    members = {address}
    members.update(milestone_client.aliases(address).get("aliases", []))
    return address, {m for m in members if m}


def get_artifact(
    session: Session,
    artifact_id: str,
    include_body: bool = True,
    milestone: str | None = None,
    milestone_client: MilestoneClient | None = None,
) -> dict:
    """The full artifact — identity, lifecycle, governs, the current leaf + all
    competing leaves + branch points — by id. With `include_body` (the default:
    this is the on-demand full record, #132) `current_version` carries the
    canonical inline content as `body_snapshot`; leaves stay lean headers
    (expand one via `get_artifact_version`). Read-only; raises on an
    unknown/invalid id (parsed first, so a non-UUID is a clean not-found).

    With a `milestone_client` injected (the wired surface), `current_version` is
    the milestone-aware CANONICAL version (leaf of the eligible subgraph, §6.1.3),
    and each version row carries its milestone `bucket`. Passing `milestone=REF`
    returns the version valid FOR that milestone (the leaf of the subgraph stamped
    with it, alias-set matched, §6.1.5); no stamped version → the canonical
    version."""
    a = _require_artifact(session, artifact_id)
    milestone_filter: set[str] | None = None
    if milestone:
        # A per-milestone read is an explicit request — with no client to
        # resolve against it must fail loudly, never silently degrade to the
        # default read (§6.1.5).
        if milestone_client is None:
            raise ValueError(
                f"a per-milestone read (milestone={milestone!r}) requires a "
                "milestone client — a clientless call has no platform to "
                "resolve the ref against"
            )
        # Bare-name context = the artifact's single governing scope (§6.1.1).
        gov_slugs = _governs(session, a.id)
        context = (
            gov_slugs[0]
            if (not a.governs_all and len(gov_slugs) == 1)
            else None
        )
        _primary, milestone_filter = _milestone_alias_set(
            milestone, milestone_client, context=context
        )
    return _artifact_dict(
        session,
        a,
        include_body=include_body,
        milestone_client=milestone_client,
        milestone_filter=milestone_filter,
    )


def get_artifact_version(
    session: Session, artifact_id: str, version_id: str
) -> dict:
    """One version's full record — including its `body_snapshot` — by
    (artifact, version) pair (#132). This is the body read for versions
    `get_artifact` keeps lean: a competing leaf (branch comparison before
    `resolve_artifact`) or a superseded version (audit / pinned exports).
    Read-only; raises when the version doesn't belong to the artifact."""
    a = _require_artifact(session, artifact_id)
    v = _require_version(session, a, version_id)
    out = _version_dict(v, include_body=True)
    out["artifact_id"] = str(a.id)
    return out


def _resolve_list_limit(limit: int | None, default: int = 50, cap: int = 500) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), cap))


def list_versions_by_milestone(
    session: Session,
    milestone: str,
    limit: int | None = None,
    milestone_client: MilestoneClient | None = None,
) -> dict:
    """List every artifact VERSION stamped with a milestone (spec §4 / §6.1.5),
    across all artifacts — the release-correlation read ("what versions did
    `v1-launch` ship as?"). Newest first, capped at `limit` (default 50, max 500)
    with `items_total` carrying the true depth. Each row is the lean version
    header (`_version_dict`, no body) PLUS its `artifact_id`; expand one via
    `get_artifact_version(artifact_id, version_id)`.

    With a `milestone_client` injected (the wired surface), the ref is RESOLVED
    and stamps are matched against the target's FULL ALIAS SET (§5) — closing the
    stored-verbatim-slug gap: a version stamped under a since-merged slug still
    surfaces, flagged `matched_via_alias`. Bare refs resolve against no context
    here (this is a portfolio-wide read, not per-artifact), so a bare ref needs
    the read to carry a full address. Clientless, it falls back to an exact
    canonical-slug equality match (#141). An empty/None milestone yields an empty
    list (there is no 'unstamped' milestone to list)."""
    lim = _resolve_list_limit(limit)
    if milestone is None or (isinstance(milestone, str) and not milestone.strip()):
        return {"milestone": milestone, "versions": [], "items_total": 0}

    if milestone_client is None:
        canonical = _validate_milestone(milestone)
        if canonical is None:
            return {"milestone": milestone, "versions": [], "items_total": 0}
        match_set = {canonical}
        primary = canonical
    else:
        # Portfolio-wide read: no single artifact to derive a bare-name context
        # from, so the ref must be a full address (`_milestone_alias_set` rejects
        # a bare ref with context=None). Unknown → the client's resolution error
        # (with suggestions) propagates. ONE resolve — the helper returns the
        # canonical primary alongside the match set.
        primary, match_set = _milestone_alias_set(
            milestone.strip(), milestone_client, context=None
        )

    stmt = (
        select(ArtifactVersion)
        .where(ArtifactVersion.milestone.in_(sorted(match_set)))
        .order_by(ArtifactVersion.created_at.desc(), ArtifactVersion.id.desc())
    )
    rows = list(session.scalars(stmt))
    versions: list[dict] = []
    for v in rows[:lim]:
        row = _version_dict(v)
        row["artifact_id"] = str(v.artifact_id)
        # A stamp matched via a NON-primary alias (a since-merged slug) is a
        # backfill candidate — flag it so an agent can re-stamp to canonical.
        if milestone_client is not None and v.milestone != primary:
            row["matched_via_alias"] = True
        versions.append(row)
    return {
        "milestone": primary,
        "versions": versions,
        "items_total": len(rows),
    }


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
