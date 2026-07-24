"""HTTP read/resolve surface for the milestone registry (milestones.md §5).

Out-of-process plugins (governance, PM) cannot import the platform, so they read
+ resolve milestones over HTTP, exactly like the scope surface:

  GET  /milestones                          list (optional ?anchor= & ?status=)
  GET  /milestones/resolve?ref=&context=    single-ref resolution (§3)
  POST /milestones/resolve-batch            {refs:[...], context?} -> per-ref
                                            {address, status, resolved_via_alias}
  POST /milestones/merge                    {from, into} merge → alias tombstone
  GET  /milestones/{address}                the row (audit read; 404 if unknown)
  GET  /milestones/{address}/aliases        the tombstone closure for a target
  GET  /milestones/{address}/dependencies   both edge directions (§4)
  POST /milestones/{address}/dependencies   {dependency} add an edge
  DELETE /milestones/{address}/dependencies?dependency=  remove an edge
  POST /milestones                          create (the only mint path)
  POST /milestones/{address}/activate|achieve|cancel   lifecycle verbs
  PATCH /milestones/{address}               update outcome / target_date

These ride behind the platform trust middleware automatically — only a trusted
principal reads/writes the registry.

Route ORDER matters (mirrors scopes_routes): `/milestones/resolve` and
`/milestones/resolve-batch` are declared BEFORE the `{address}` routes, and
`{address}` uses the `:path` converter (an address contains `/`, e.g.
`org/repo/name`) so a multi-segment address resolves to one path param.

The MCP tool wrappers over this service now exist too — served on the platform
`main` surface via self-registration (decision 0503fff0; `platform_tools.py`),
not from this HTTP router.

Increment 2 adds `merge` (+ the `/aliases` closure read) and the dependency
verbs (+ the `/dependencies` read/add/remove routes). Route ORDER: the
`/aliases` and `/dependencies` GET suffixes are declared BEFORE the bare
`/{address}` catch-all (same hazard as `/transitions`); `/merge` is a fixed POST
path with no bare-address POST to shadow it. DEFERRED (see the PR): replication
emission (§9), the governance consumer (§6.1), and readiness computation itself
(PM-side, over these reads).
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from snowline_platform import milestones
from snowline_platform.db import session_scope

router = APIRouter(prefix="/milestones", tags=["milestones"])


def get_session() -> Session:
    """Per-request DB session (commits on success, rolls back on error)."""
    with session_scope() as s:
        yield s


@router.get("")
async def list_milestones(
    request: Request,
    anchor: str | None = None,
    status: str | None = None,
    include_merged: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    return {
        "milestones": milestones.list_milestones(
            session, anchor=anchor, status=status, include_merged=include_merged
        )
    }


@router.get("/resolve")
async def resolve_milestone(
    request: Request,
    ref: str,
    context: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    try:
        milestone, via_alias = milestones.resolve_row(session, ref, context)
    except milestones.MilestoneResolutionError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"detail": str(exc), "suggestions": exc.suggestions},
        ) from None
    except (
        milestones.InvalidMilestoneNameError,
        milestones.scopes.InvalidSlugError,
    ) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)
        ) from None
    row = milestones.to_row(milestone)
    row["resolved_via_alias"] = via_alias
    return row


@router.post("/resolve-batch")
async def resolve_batch(
    request: Request,
    refs: list[str] = Body(..., embed=True),
    context: str | None = Body(None),
    session: Session = Depends(get_session),
) -> dict:
    """Resolve many refs in one round-trip (§5) — the read governance's
    canonicality computation uses, so per-stamp fan-out is one call. Each ref
    maps to `{address, status, resolved_via_alias}` on success, or `{error}` on
    a miss — a single unresolvable ref never fails the whole batch (governance
    buckets an unresolvable stamp as legacy, §6.1.2). `resolved_via_alias` is
    always false in this increment (merge deferred)."""
    results: dict[str, dict] = {}
    for ref in refs:
        try:
            milestone, via_alias = milestones.resolve_row(session, ref, context)
        except milestones.MilestoneResolutionError as exc:
            results[ref] = {"error": str(exc), "suggestions": exc.suggestions}
            continue
        except (
            milestones.InvalidMilestoneNameError,
            milestones.scopes.InvalidSlugError,
        ) as exc:
            results[ref] = {"error": str(exc)}
            continue
        results[ref] = {
            "address": milestones.address_of(milestone),
            "status": milestone.status,
            "resolved_via_alias": via_alias,
        }
    return {"results": results}


@router.post("/merge")
async def merge_milestone(
    request: Request,
    payload: dict = Body(...),
    session: Session = Depends(get_session),
) -> dict:
    """Merge `from` into `into` (§7) — a fixed path (`from` is a reserved word, so
    the body carries `{from, into}` rather than typed params). `into` resolves to
    its terminal target; the tombstone stays at `from`'s anchor. Returns
    `{tombstone, target, reminder}`."""
    missing = {"from", "into"} - set(payload)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"merge requires {sorted({'from', 'into'})}; missing {sorted(missing)}",
        )
    try:
        return milestones.merge(session, payload["from"], payload["into"])
    except milestones.MilestoneNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except milestones.MilestoneResolutionError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"detail": str(exc), "suggestions": exc.suggestions},
        ) from None
    except milestones.MilestoneMergeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_milestone(
    request: Request,
    anchor: str = Body(...),
    name: str = Body(...),
    outcome: str | None = Body(None),
    target_date: date | None = Body(None),
    session: Session = Depends(get_session),
) -> dict:
    try:
        milestone = milestones.create(
            session,
            anchor=anchor,
            name=name,
            outcome=outcome,
            target_date=target_date,
        )
    except milestones.MilestoneConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except (
        milestones.InvalidAnchorError,
        milestones.InvalidMilestoneNameError,
        milestones.InvalidMilestoneFieldError,
    ) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)
        ) from None
    return milestones.to_row(milestone)


def _lifecycle(verb):
    async def handler(
        address: str,
        request: Request,
        reason: str | None = Body(None, embed=True),
        session: Session = Depends(get_session),
    ) -> dict:
        try:
            milestone = verb(session, address, reason=reason)
        except milestones.MilestoneNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
        except milestones.IllegalTransitionError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        return milestones.to_row(milestone)

    return handler


router.add_api_route(
    "/{address:path}/activate",
    _lifecycle(milestones.activate),
    methods=["POST"],
)
router.add_api_route(
    "/{address:path}/achieve",
    _lifecycle(milestones.achieve),
    methods=["POST"],
)
router.add_api_route(
    "/{address:path}/cancel",
    _lifecycle(milestones.cancel),
    methods=["POST"],
)


@router.get("/{address:path}/transitions")
async def milestone_transitions(
    address: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    try:
        log = milestones.transitions(session, address)
    except milestones.MilestoneNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    return {"transitions": log}


@router.get("/{address:path}/aliases")
async def milestone_aliases(
    address: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """The tombstone closure resolving to `address`'s terminal target (§5)."""
    try:
        return milestones.aliases(session, address)
    except milestones.MilestoneResolutionError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"detail": str(exc), "suggestions": exc.suggestions},
        ) from None


@router.get("/{address:path}/dependencies")
async def milestone_dependencies(
    address: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Both directions of a milestone's dependency edges (§4)."""
    try:
        return milestones.dependencies(session, address)
    except milestones.MilestoneResolutionError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"detail": str(exc), "suggestions": exc.suggestions},
        ) from None


def _dependency_edit(verb):
    async def handler(
        address: str,
        request: Request,
        dependency: str,
        session: Session = Depends(get_session),
    ) -> dict:
        try:
            return verb(session, address, dependency)
        except milestones.MilestoneResolutionError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                {"detail": str(exc), "suggestions": exc.suggestions},
            ) from None
        except milestones.MilestoneDependencyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    return handler


# POST reads `dependency` from the body; DELETE reads it from the query string
# (a request body on DELETE is non-standard). Both are edge verbs on the
# dependent named by `{address}`.
async def _add_dependency(
    address: str,
    request: Request,
    dependency: str = Body(..., embed=True),
    session: Session = Depends(get_session),
) -> dict:
    return await _dependency_edit(milestones.add_dependency)(
        address, request, dependency, session
    )


router.add_api_route(
    "/{address:path}/dependencies", _add_dependency, methods=["POST"]
)
router.add_api_route(
    "/{address:path}/dependencies",
    _dependency_edit(milestones.remove_dependency),
    methods=["DELETE"],
)


# Declared AFTER the `/{address}/transitions`, `/aliases`, `/dependencies` suffix
# routes so the greedy `:path` catch-all cannot shadow them (mirrors
# scopes_routes' /ancestors-before-/{slug} ordering). PATCH/POST/DELETE verbs
# above don't conflict with this GET (distinct methods).
@router.get("/{address:path}")
async def get_milestone(
    address: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    try:
        milestone = milestones.get(session, address)
    except milestones.MilestoneNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    return milestones.to_row(milestone)


@router.patch("/{address:path}")
async def update_milestone(
    address: str,
    request: Request,
    payload: dict = Body(...),
    session: Session = Depends(get_session),
) -> dict:
    # PATCH semantics: only keys PRESENT in the body change; a present key with
    # a null value CLEARS that field (the service's _UNSET-vs-None distinction —
    # typed Body(None) params can't express "omitted", they'd clear on every
    # partial update).
    unknown = set(payload) - {"outcome", "target_date"}
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown milestone fields: {sorted(unknown)}",
        )
    kwargs: dict = {}
    if "outcome" in payload:
        kwargs["outcome"] = payload["outcome"]
    if "target_date" in payload:
        raw = payload["target_date"]
        try:
            kwargs["target_date"] = (
                date.fromisoformat(raw) if raw is not None else None
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"invalid target_date: {raw!r}",
            ) from exc
    try:
        milestone = milestones.update(session, address, **kwargs)
    except milestones.MilestoneNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    return milestones.to_row(milestone)
