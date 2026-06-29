"""The scope service — the platform-owned operations over the `Scope` tree.

Carried (functionality-first, NOT imported) from the frozen monolith's
`snowline_server.graph`: `get_scope`, `ancestor_scopes_until_isolated`,
`scope_tree`, `list_scopes`, plus the bare-slug⇔org invariant + slug-derived
`parent_id` from `update_scope`. The platform owns scopes (architecture.md §2);
this is the read/resolve + create surface the HTTP API and MCP tools wrap.

`resolve` is NON-MUTATING (spec §3 "auto-vivify"): the monolith's
`resolve_or_stub` convenience is intentionally NOT carried into the public read
path — creation is explicit via `create`.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from snowline_platform.models import Scope

# --- slug / kind contract (carried from the monolith parser, §2.1) ----------

# Allowed scope kinds. `org` is the bare-org-segment scope (no `/`).
VALID_KINDS = {"project", "component", "topic", "initiative", "org"}

# §2.1 slug regex: a bare org segment or `<org>/<rest>...`. A segment is the
# GitHub identifier set lowercased — `[a-z0-9._-]` with at least one alphanumeric
# (leading punctuation allowed, e.g. `.github`). Rejects uppercase, spaces, empty
# or all-punctuation segments. Linear-time (disjoint leading/required classes).
_SLUG_SEG = r"[._-]*[a-z0-9][a-z0-9._-]*"
SLUG_RE = re.compile(rf"^{_SLUG_SEG}(/{_SLUG_SEG})*$")


class InvalidSlugError(ValueError):
    """Slug violates the §2.1 convention."""


class InvalidScopeFieldError(ValueError):
    """A field value is not valid for a scope."""


class ScopeNotFoundError(LookupError):
    """No scope with the given slug."""


class ScopeConflictError(ValueError):
    """A scope with the given slug already exists."""


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise InvalidSlugError(f"invalid scope slug: {slug!r} (§2.1)")
    return slug


def kind_matches_slug(slug: str, kind: str) -> bool:
    """The bare-slug ⇔ kind 'org' invariant (spec §2): a bare org slug (no `/`)
    must be kind `org`, and `org` is valid only for a bare slug."""
    return (kind == "org") == ("/" not in slug)


# --- read / resolve ---------------------------------------------------------


def resolve(session: Session, slug: str) -> Scope | None:
    """Non-mutating lookup — the scope for `slug`, or None if unknown (spec §3).
    No implicit stub creation in the public read path."""
    return session.scalar(select(Scope).where(Scope.slug == slug))


# Back-compat alias for the monolith's name; `resolve` is the spec verb.
get_scope = resolve


def ancestors(session: Session, scope: Scope) -> list[Scope]:
    """`scope` then each `parent_id` ancestor, nearest-first, HALTING at the
    first `isolated` node and at the forest root (spec §3, §5; carried from
    `graph.ancestor_scopes_until_isolated`).

    This is the scope tree's *applicability* walk: a reader at X resolves X's own
    plus every ancestor's governance UPWARD only, stopping the moment it crosses
    an `isolated` boundary — an `isolated` scope blocks inheritance from ABOVE it.
    The first isolated node is itself included (its own + below still resolve) but
    its parent is not reached. A visited guard makes a malformed `parent_id`
    cycle terminate instead of looping.
    """
    chain: list[Scope] = []
    seen: set[uuid.UUID] = set()
    node: Scope | None = scope
    while node is not None and node.id not in seen:
        chain.append(node)
        seen.add(node.id)
        # Stop once we reach an isolated node (already collected) or the root.
        # An isolated node blocks inheritance from above, so we do NOT ascend.
        if node.isolated or node.parent_id is None:
            break
        node = session.get(Scope, node.parent_id)
    return chain


def list_scopes(session: Session, org: str | None = None) -> list[dict]:
    """All scopes as lightweight rows (slug, name, kind, derived org, status,
    isolated), slug-ordered. `org` narrows to one org (the first slug segment).
    Read-only (carried from `graph.list_scopes`, with `isolated` exposed)."""
    out: list[dict] = []
    for sc in session.scalars(select(Scope).order_by(Scope.slug.asc())):
        scope_org = sc.slug.split("/", 1)[0]
        if org is not None and scope_org != org:
            continue
        out.append(
            {
                "slug": sc.slug,
                "name": sc.name,
                "kind": sc.kind,
                "org": scope_org,
                "status": sc.status,
                "isolated": sc.isolated,
            }
        )
    return out


def tree(session: Session, root: str | None = None) -> list[dict]:
    """The scope forest as nested `parent_id`-edged trees (spec §3, §5; carried
    from `graph.scope_tree`). Each node is
    `{slug, name, kind, status, isolated, children}`, slug-ordered. Follows the
    real `parent_id` edges — the AUTHORITATIVE applicability mechanism; `isolated`
    is exposed on every node (the inheritance boundary a reader reasons about).

    `root` (a slug) returns just that scope's subtree; omit it for the whole
    forest — every scope with no `parent_id`, OR whose `parent_id` points at a
    scope absent from the set (a dangling parent), is a forest root, so no scope
    is silently dropped. A `seen` guard makes a malformed `parent_id` cycle
    terminate instead of recursing forever. Raises on an unknown `root`.
    """
    scopes = list(session.scalars(select(Scope).order_by(Scope.slug.asc())))
    children: dict = {}
    for sc in scopes:
        children.setdefault(sc.parent_id, []).append(sc)

    seen: set[uuid.UUID] = set()

    def node(sc: Scope) -> dict:
        seen.add(sc.id)
        return {
            "slug": sc.slug,
            "name": sc.name,
            "kind": sc.kind,
            "status": sc.status,
            "isolated": sc.isolated,
            "children": [
                node(c) for c in children.get(sc.id, []) if c.id not in seen
            ],
        }

    if root is not None:
        r = resolve(session, root)
        if r is None:
            raise ScopeNotFoundError(
                f"unknown scope slug: {root!r} — register it or check the slug"
            )
        return [node(r)]
    present = {sc.id for sc in scopes}
    return [
        node(sc)
        for sc in scopes
        if sc.parent_id is None or sc.parent_id not in present
    ]


# --- create / update --------------------------------------------------------


def _validate_kind_for_slug(slug: str, kind: str) -> None:
    if kind not in VALID_KINDS:
        raise InvalidScopeFieldError(f"invalid kind: {kind!r}")
    is_bare = "/" not in slug
    if kind == "org" and not is_bare:
        raise InvalidScopeFieldError(
            f"kind 'org' is only valid for a bare org slug (no '/'): {slug!r}"
        )
    if kind != "org" and is_bare:
        raise InvalidScopeFieldError(
            f"a bare org slug must be kind 'org', not {kind!r}: {slug!r}"
        )


def create(
    session: Session,
    slug: str,
    name: str,
    kind: str,
    *,
    parent: str | None = None,
    isolated: bool = False,
    status: str = "active",
) -> Scope:
    """Create a scope (spec §3). Enforces the bare-slug⇔org invariant and
    derives/validates `parent_id` from the slug hierarchy: an explicit `parent`
    slug is resolved (must exist); otherwise a hierarchical slug's parent is its
    `rsplit('/', 1)[0]` prefix, linked to that ROW if it exists (the slug
    hierarchy and `parent_id` are kept consistent — spec §5).

    Raises `ScopeConflictError` if the slug is already taken, `InvalidSlugError`
    / `InvalidScopeFieldError` on a bad slug/kind/parent.
    """
    validate_slug(slug)
    _validate_kind_for_slug(slug, kind)
    if not isinstance(isolated, bool):
        raise InvalidScopeFieldError(f"isolated must be bool: {isolated!r}")
    if resolve(session, slug) is not None:
        raise ScopeConflictError(f"scope {slug!r} already exists")

    is_bare = "/" not in slug
    parent_id: uuid.UUID | None = None
    if kind == "org":
        # An org is the top of the tree — it has no parent.
        if parent:
            raise InvalidScopeFieldError(
                f"an org scope has no parent (got {parent!r} for {slug!r})"
            )
    elif parent:
        validate_slug(parent)
        prow = resolve(session, parent)
        if prow is None:
            raise ScopeNotFoundError(f"parent scope {parent!r} does not exist")
        parent_id = prow.id
    elif not is_bare:
        # Derive the parent from the slug's prefix; link to the row if present.
        prow = resolve(session, slug.rsplit("/", 1)[0])
        if prow is not None:
            parent_id = prow.id

    scope = Scope(
        slug=slug,
        name=name,
        kind=kind,
        parent_id=parent_id,
        isolated=isolated,
        status=status,
    )
    session.add(scope)
    session.flush()
    return scope


_UNSET = object()


def update(
    session: Session,
    slug: str,
    *,
    name: str | None = None,
    kind: str | None = None,
    parent=_UNSET,
    isolated: bool | None = None,
    status: str | None = None,
) -> Scope:
    """Modify an existing scope (spec §3). Validates the bare-slug⇔org invariant
    on `kind`; `parent` ("" / None clears, a slug re-points to that existing row)
    keeps `parent_id` consistent. Raises `ScopeNotFoundError` if unknown."""
    scope = resolve(session, slug)
    if scope is None:
        raise ScopeNotFoundError(f"no scope with slug {slug!r}")

    if kind is not None:
        _validate_kind_for_slug(slug, kind)
        scope.kind = kind
    if name is not None:
        scope.name = name
    if isolated is not None:
        if not isinstance(isolated, bool):
            raise InvalidScopeFieldError(f"isolated must be bool: {isolated!r}")
        scope.isolated = isolated
    if status is not None:
        scope.status = status
    if parent is not _UNSET:
        if parent in (None, ""):
            scope.parent_id = None
        else:
            if (kind or scope.kind) == "org":
                raise InvalidScopeFieldError(
                    f"an org scope has no parent (got {parent!r} for {slug!r})"
                )
            validate_slug(parent)
            prow = resolve(session, parent)
            if prow is None:
                raise ScopeNotFoundError(
                    f"parent scope {parent!r} does not exist"
                )
            scope.parent_id = prow.id

    session.flush()
    return scope


# --- serialization (the HTTP/MCP JSON shape) --------------------------------


def to_row(scope: Scope) -> dict:
    """A scope as a flat JSON row (the read API's `GET /scopes/{slug}` shape).

    Carries `id` (the scope's UUID, stringified) so an out-of-process plugin can
    capture it as a SOFT reference on its own rows (governance stores `scope_id`
    on each decision for monolith schema-compat) without a second round-trip."""
    return {
        "id": str(scope.id),
        "slug": scope.slug,
        "name": scope.name,
        "kind": scope.kind,
        "status": scope.status,
        "isolated": scope.isolated,
        "org": scope.slug.split("/", 1)[0],
    }
