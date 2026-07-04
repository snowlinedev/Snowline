"""HTTP read/resolve surface for the scope namespace (spec §4).

Out-of-process plugins (governance, memory) cannot import the platform, so they
fetch the scope tree over HTTP:

  GET  /scopes                      list (optional ?org=)
  GET  /scopes/tree                 nested forest (optional ?root=)
  GET  /scopes/{slug}               resolve one (404 if unknown)
  GET  /scopes/{slug}/ancestors     the isolation-halting applicability chain
  POST /scopes                      create

These ride behind the platform trust middleware automatically (not exempt) —
only a trusted principal reads/writes the tree.

Route ORDER matters: `/scopes/tree` is declared before the `{slug}` routes, and
`{slug}` uses the `:path` converter (a slug contains `/`, e.g. `org/repo/init`),
so a multi-segment slug resolves to one path param rather than 404-ing.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from snowline_platform import scopes
from snowline_platform.db import session_scope

router = APIRouter(prefix="/scopes", tags=["scopes"])


def get_session() -> Session:
    """Per-request DB session (commits on success, rolls back on error)."""
    with session_scope() as s:
        yield s


@router.get("")
async def list_scopes(
    request: Request,
    org: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    return {"scopes": scopes.list_scopes(session, org=org)}


@router.get("/tree")
async def scope_tree(
    request: Request,
    root: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    try:
        forest = scopes.tree(session, root=root)
    except scopes.ScopeNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    return {"tree": forest}


@router.get("/{slug:path}/ancestors")
async def scope_ancestors(
    slug: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    scope = scopes.resolve(session, slug)
    if scope is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no scope with slug {slug!r}"
        )
    chain = scopes.ancestors(session, scope)
    return {"ancestors": [scopes.to_row(s) for s in chain]}


@router.get("/{slug:path}")
async def get_scope(
    slug: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    scope = scopes.resolve(session, slug)
    if scope is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no scope with slug {slug!r}"
        )
    return scopes.to_row(scope)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_scope(
    request: Request,
    slug: str = Body(...),
    name: str = Body(...),
    kind: str = Body(...),
    parent: str | None = Body(None),
    isolated: bool = Body(False),
    session: Session = Depends(get_session),
) -> dict:
    try:
        scope = scopes.create(
            session,
            slug=slug,
            name=name,
            kind=kind,
            parent=parent,
            isolated=isolated,
        )
    except scopes.ScopeConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except scopes.ScopeNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except (scopes.InvalidSlugError, scopes.InvalidScopeFieldError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)
        ) from None
    return scopes.to_row(scope)
