"""Real-graph corpus search (#170) — full-text over the governing artifacts and
the decision graph, the `shadow_corpus_search` pattern applied to the REAL side.

Two corpora, merged and ranked:

  * `artifact` — the CURRENT structural leaf version (derived title + body) of
    every live artifact. Superseded artifacts (#166) and non-current versions
    are excluded — a hit is always the content `get_artifact` would serve, so
    search never resurrects retired or historical prose. Structural (not
    milestone-aware) canonicality: a hit is a pointer, and the milestone-aware
    read happens on the follow-up `get_artifact`.
  * `decision` — current decision leaves (statement + rationale); superseded
    predecessors stay searchable only through their superseding leaf, matching
    the default read posture everywhere else.

Row shape mirrors the shadow tool (`{kind, id, snippet, rank}` + per-kind
identity: artifacts carry `title` + `governs`/`governs_all`, decisions carry
`scope`). `scope_id` (resolved by the MCP surface) narrows each corpus: a
decision matches its exact scope; an artifact matches via a governs edge OR
`governs_all` — the same matching `list_artifacts(governs=...)` uses. The
query is ALWAYS a bound parameter (func args bind), never interpolated.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select, true
from sqlalchemy.orm import Session

from snowline_governance import branching
from snowline_governance.models import (
    Artifact,
    ArtifactGoverns,
    ArtifactVersion,
    Decision,
)

SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 100
_TS_CONFIG = "english"
_KIND_ORDER = ("artifact", "decision")


def _resolve_limit(limit: int | None) -> int:
    if limit is None:
        return SEARCH_DEFAULT_LIMIT
    return max(1, min(int(limit), SEARCH_MAX_LIMIT))


def _artifact_stmt_parts(scope_id: uuid.UUID | None):
    """The artifact corpus: current-leaf versions of live artifacts. The leaf
    sub-select runs GLOBALLY (constraint `true()`) — supersession is
    intra-artifact by construction, so a global NOT-IN is equivalent to the
    per-artifact filter `_leaf_stmt` applies, in one query."""
    text = func.concat_ws(
        " ", ArtifactVersion.title, ArtifactVersion.body_snapshot
    )
    joins = [(Artifact, Artifact.id == ArtifactVersion.artifact_id)]
    filters = [
        ArtifactVersion.status != "superseded",
        branching.leaf_filter(
            ArtifactVersion.id, ArtifactVersion.supersedes_id, true()
        ),
        Artifact.superseded_by_id.is_(None),
    ]
    if scope_id is not None:
        governing = select(ArtifactGoverns.artifact_id).where(
            ArtifactGoverns.scope_id == scope_id
        )
        filters.append(
            or_(Artifact.id.in_(governing), Artifact.governs_all.is_(True))
        )
    return text, joins, filters


def _decision_stmt_parts(scope_id: uuid.UUID | None):
    """The decision corpus: current leaves only (global NOT-IN — supersession
    is intra-scope, so this equals the per-scope leaf filter)."""
    text = func.concat_ws(" ", Decision.decision, Decision.rationale)
    filters = [
        branching.leaf_filter(Decision.id, Decision.supersedes_id, true())
    ]
    if scope_id is not None:
        filters.append(Decision.scope_id == scope_id)
    return text, [], filters


def corpus_search(
    session: Session,
    query: str,
    scope_id: uuid.UUID | str | None = None,
    scope_slug: str | None = None,
    limit: int | None = None,
) -> dict:
    """Ranked full-text search over the real governance graph — see the module
    docstring for corpus and shape. Raises `ValueError` on a blank query."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("`query` must be a non-empty string")
    sid = None
    if scope_id is not None:
        sid = (
            scope_id
            if isinstance(scope_id, uuid.UUID)
            else uuid.UUID(str(scope_id))
        )
    lim = _resolve_limit(limit)
    tsq = func.websearch_to_tsquery(_TS_CONFIG, query)

    rows: list[dict] = []
    total = 0

    # --- artifact corpus ------------------------------------------------------
    text, joins, filters = _artifact_stmt_parts(sid)
    vec = func.to_tsvector(_TS_CONFIG, text)
    match = vec.op("@@")(tsq)
    rank = func.ts_rank(vec, tsq)
    snippet = func.ts_headline(_TS_CONFIG, text, tsq)
    stmt = select(
        Artifact.id, ArtifactVersion.title, snippet, rank
    ).select_from(ArtifactVersion)
    for join in joins:
        stmt = stmt.join(*join)
    stmt = (
        stmt.where(match, *filters)
        .order_by(rank.desc(), Artifact.id.asc())
        .limit(lim)  # top-lim per kind ⊇ the merged top-lim
    )
    art_rows = session.execute(stmt).all()
    # ONE batched governs read for the matched artifacts (the compact-row
    # labeling, not a per-row query).
    art_ids = [rid for rid, _t, _s, _r in art_rows]
    governs_map: dict[uuid.UUID, list[str]] = {aid: [] for aid in art_ids}
    governs_all: dict[uuid.UUID, bool] = {}
    if art_ids:
        for aid, slug in session.execute(
            select(ArtifactGoverns.artifact_id, ArtifactGoverns.scope_slug)
            .where(ArtifactGoverns.artifact_id.in_(art_ids))
            .order_by(ArtifactGoverns.scope_slug.asc())
        ):
            governs_map.setdefault(aid, []).append(slug)
        for aid, gall in session.execute(
            select(Artifact.id, Artifact.governs_all).where(
                Artifact.id.in_(art_ids)
            )
        ):
            governs_all[aid] = gall
    rows.extend(
        {
            "kind": "artifact",
            "id": str(rid),
            "title": rtitle,
            "governs": governs_map.get(rid, []),
            "governs_all": governs_all.get(rid, False),
            "snippet": rsnippet,
            "rank": float(rrank),
        }
        for rid, rtitle, rsnippet, rrank in art_rows
    )
    cstmt = select(func.count()).select_from(ArtifactVersion)
    for join in joins:
        cstmt = cstmt.join(*join)
    total += session.scalar(cstmt.where(match, *filters)) or 0

    # --- decision corpus ------------------------------------------------------
    text, _joins, filters = _decision_stmt_parts(sid)
    vec = func.to_tsvector(_TS_CONFIG, text)
    match = vec.op("@@")(tsq)
    rank = func.ts_rank(vec, tsq)
    snippet = func.ts_headline(_TS_CONFIG, text, tsq)
    stmt = (
        select(Decision.id, Decision.scope_slug, snippet, rank)
        .where(match, *filters)
        .order_by(rank.desc(), Decision.id.asc())
        .limit(lim)
    )
    rows.extend(
        {
            "kind": "decision",
            "id": str(rid),
            "scope": rscope,
            "snippet": rsnippet,
            "rank": float(rrank),
        }
        for rid, rscope, rsnippet, rrank in session.execute(stmt)
    )
    total += (
        session.scalar(
            select(func.count()).select_from(Decision).where(match, *filters)
        )
        or 0
    )

    order = {k: i for i, k in enumerate(_KIND_ORDER)}
    rows.sort(key=lambda r: (-r["rank"], order[r["kind"]], r["id"]))
    return {
        "query": query,
        "scope": scope_slug if sid is not None else None,
        "kinds": list(_KIND_ORDER),
        "results": rows[:lim],
        "results_total": total,
    }
