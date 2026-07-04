"""The scope service — the platform-owned operations over the `Scope` tree.

Carried (functionality-first, NOT imported) from the frozen monolith's
`snowline_server.graph`: `get_scope`, `ancestor_scopes_until_isolated`,
`scope_tree`, `list_scopes`, plus the bare-slug⇔org invariant + slug-derived
`parent_id` from `update_scope`. The platform owns scopes (architecture.md §2);
this is the read/resolve + create surface the HTTP API and MCP tools wrap.

`resolve` is NON-MUTATING (spec §3 "auto-vivify"): the monolith's
`resolve_or_stub` convenience is intentionally NOT carried into the public read
path — creation is explicit via `create`.

**Replication (spec §8, issue #81):** the platform dogfoods the same SDK
emit/ingest modules it offers plugins — `create`/`update` emit
`scope.created`/`scope.updated` in the SAME transaction as the domain write
(the transactional outbox, §3), and `apply_scope_event` is the domain APPLY
function an opted-in stream runs deliveries through (`replication.py` wires it
into the SDK's ingest route). Both directions reuse `create`/`update` rather
than a separate write path, so their EXISTING exceptions are the ordering/
collision error taxonomy §8 needs — see `apply_scope_event`'s docstring.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from snowline_platform.models import Scope
from snowline_plugin_sdk.contract import EVENT_SCOPE_CREATED, EVENT_SCOPE_UPDATED
from snowline_plugin_sdk.replication import emit_event

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


# A sentinel distinguishing "not provided" (derive/leave-as-is) from an
# EXPLICIT value, including explicit `None` — `create`/`update` both need this
# (see each's `parent` docs); shared so replication's apply seam and any
# future caller can rely on one identity.
_UNSET = object()


def create(
    session: Session,
    slug: str,
    name: str,
    kind: str,
    *,
    parent: str | None = _UNSET,
    isolated: bool = False,
    status: str = "active",
    scope_id: uuid.UUID | None = None,
) -> Scope:
    """Create a scope (spec §3). Enforces the bare-slug⇔org invariant and
    resolves `parent_id`:

      * NOT PROVIDED (`_UNSET`, the default) — derive from the slug's
        hierarchical `rsplit('/', 1)[0]` prefix, linking to that ROW if it
        exists, else leaving `parent_id` None (the legacy convenience human/API
        callers rely on — the slug hierarchy and `parent_id` stay consistent,
        spec §5).
      * an explicit SLUG — resolved; must already exist.
      * explicit `None` (or `""`) — NO parent, NO derivation. This is what
        `apply_scope_event` always passes: a replicated scope's `parent_id`
        must replay the ORIGIN's own resolved value verbatim. Without this
        distinction from `_UNSET`, a replica that happens to hold an
        UNRELATED local scope matching the slug's prefix would silently
        derive-attach it — a permanent `parent_id` divergence between
        instances for the SAME scope UUID that poisons §6.1's ancestor walk
        (a fresh-eyes review on #87 caught this before it shipped).

    Emits `scope.created` (spec §8) in this SAME transaction — a no-op until a
    replication subscription exists (§9 item 5/6, pairing not yet built).

    `scope_id` is the replication apply seam (`apply_scope_event`): a
    spoke-authored scope keeps its ORIGIN-side UUID on every instance, since
    plugins reference scopes by that id. Human/API callers never pass it — a
    fresh id is minted as usual.

    Raises `ScopeConflictError` if the slug is already taken (spec §8: this is
    ALSO the cross-partition slug-collision error a replicated create surfaces
    — see `apply_scope_event`), `InvalidSlugError` / `InvalidScopeFieldError` on
    a bad slug/kind/parent.
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
        # An org is the top of the tree — an explicit, non-empty parent is a
        # caller error either way; `_UNSET` (nothing given) is fine.
        if parent is not _UNSET and parent:
            raise InvalidScopeFieldError(
                f"an org scope has no parent (got {parent!r} for {slug!r})"
            )
    elif parent is _UNSET:
        # Not provided: derive from the slug's prefix, linking to that row
        # if present — the legacy convenience for ordinary callers.
        if not is_bare:
            prow = resolve(session, slug.rsplit("/", 1)[0])
            if prow is not None:
                parent_id = prow.id
    elif parent:
        # An explicit, non-empty parent slug: must already exist.
        validate_slug(parent)
        prow = resolve(session, parent)
        if prow is None:
            raise ScopeNotFoundError(f"parent scope {parent!r} does not exist")
        parent_id = prow.id
    # else: parent is explicitly None/"" — parent_id stays None, NO derivation.

    kwargs = dict(
        slug=slug,
        name=name,
        kind=kind,
        parent_id=parent_id,
        isolated=isolated,
        status=status,
    )
    if scope_id is not None:
        kwargs["id"] = scope_id
    scope = Scope(**kwargs)
    session.add(scope)
    session.flush()
    emit_event(session, EVENT_SCOPE_CREATED, to_replication_payload(scope))
    return scope


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
    keeps `parent_id` consistent. Raises `ScopeNotFoundError` if unknown.

    Emits `scope.updated` (spec §8) in this SAME transaction, same as `create`."""
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
    emit_event(session, EVENT_SCOPE_UPDATED, to_replication_payload(scope))
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


# --- replication (spec §8, issue #81) ---------------------------------------


def to_replication_payload(scope: Scope) -> dict:
    """The `scope.created` / `scope.updated` event body — the domain `payload`
    the SDK envelope nests (envelope.py `build_envelope`).

    Keyed by `id` (the UUID every plugin's soft reference travels by) and by
    the PARENT'S SLUG, not `parent_id`: a receiver's `parent_id` is a LOCAL row
    id that means nothing on the wire, but slug is stable across instances
    (never reused, spec §8) and reusing `create`/`update`'s existing
    parent-slug resolution is what turns an out-of-order parent into the
    ordinary retryable ordering gap spec §8's note describes, with no new
    machinery."""
    return {
        "id": str(scope.id),
        "slug": scope.slug,
        "name": scope.name,
        "kind": scope.kind,
        "parent": scope.parent.slug if scope.parent is not None else None,
        "isolated": scope.isolated,
        "status": scope.status,
    }


def apply_scope_event(session: Session, envelope: dict) -> None:
    """The platform's replication APPLY function (spec §8) — the seam
    `replication.py` wires into the SDK's `ingest_delivery`
    (`snowline_plugin_sdk.replication.ingest`). Runs under origin suppression,
    so `create`/`update`'s own `emit_event` call is a no-op here (§3.2 hard
    rule) — replicated writes can never boomerang back onto the wire.

    Reusing `create`/`update` rather than a separate write path means their
    EXISTING exceptions ARE the §8 error taxonomy — no new exception classes:

      * `ScopeConflictError` (the slug is already taken by a DIFFERENT id) —
        the cross-partition slug collision spec §8 calls out by name. Every
        apply exception is §8.1-retryable: this one will never resolve
        itself by waiting (the colliding slug does not stop colliding), but
        it still goes through the SAME bounded retry-then-park path as any
        other apply failure, because parking already IS spec §8's "fail
        loud, manual resolution" — first-class state (tool/UI/health signal,
        never a log line) that stays re-appliable once an operator renames
        or retires the losing scope and calls `reapply_parked`. Special-
        casing an immediate, un-retried park would need new machinery the
        SDK doesn't have (§8.1 has one bound, not a per-error-class one) for
        a case that is `acceptably rare for a single owner` (spec §8)
        exactly because it CAN afford to wait out the same bound as any
        other apply error.
      * `ScopeNotFoundError` (the parent slug hasn't replicated yet) — an
        ordering gap, not a failure (spec §8's ordering note): retries the
        same way and self-heals the moment the parent's own `scope.created`
        applies, so ordinary scope-stream lag never drops data.

    Idempotent past the gate (checklist item 4, §4): a payload `id` already
    present locally is a no-op — covers `reapply_parked` replay and any
    future re-delivery ambiguity, even though the SDK's watermark already
    keeps a live stream from re-invoking apply for an already-applied seq.
    """
    payload = envelope["payload"]
    event_type = envelope["event_type"]
    scope_id = uuid.UUID(payload["id"])

    if event_type == EVENT_SCOPE_CREATED:
        if session.get(Scope, scope_id) is not None:
            return  # already applied — idempotent replay
        create(
            session,
            slug=payload["slug"],
            name=payload["name"],
            kind=payload["kind"],
            parent=payload["parent"],
            isolated=payload["isolated"],
            status=payload["status"],
            scope_id=scope_id,
        )
    elif event_type == EVENT_SCOPE_UPDATED:
        existing = resolve(session, payload["slug"])
        if existing is not None and existing.id != scope_id:
            raise ScopeConflictError(
                f"scope {payload['slug']!r} update targets id {scope_id} but "
                f"the local row under that slug has id {existing.id} — "
                f"cross-partition slug collision (spec §8)"
            )
        update(
            session,
            payload["slug"],
            name=payload["name"],
            kind=payload["kind"],
            parent=payload["parent"],
            isolated=payload["isolated"],
            status=payload["status"],
        )
    else:
        raise ValueError(
            f"scope replication apply: unknown event_type {event_type!r}"
        )
