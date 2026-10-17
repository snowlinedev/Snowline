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

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from snowline_plugin_sdk.contract import EVENT_SCOPE_CREATED, EVENT_SCOPE_UPDATED
from snowline_plugin_sdk.replication import ParkNow, emit_event, is_backfill_envelope
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from snowline_platform.models import Scope

log = logging.getLogger("snowline_platform.scopes")


def _utcnow() -> datetime:
    """Naive UTC — the scope LWW clock's house convention (§7.1). Scope rows'
    `created_at`/`updated_at` used to be DB `now()` defaults, which on a
    `timestamp without time zone` column is the SERVER's local wall time — not
    comparable across two instances in different time zones. Local writes now
    stamp both explicitly in UTC (legacy rows keep their old stamps)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_stamp(value) -> datetime | None:
    """A payload stamp back to naive UTC (tolerates an aware ISO string)."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt

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


def canonical_slug(slug):
    """Fold slug INPUT to the canonical §2.1 form — lowercase, trimmed (#134).
    Slug input is case-insensitive across every platform surface (GitHub owner
    casing like `TurtlesEdge/turtletracks` is common in the wild); STORAGE and
    equality stay canonical-lowercase-only. ASCII-ONLY folding: a non-ASCII
    input passes through untouched so the §2.1 grammar still rejects it LOUDLY
    — Unicode case-folds like U+212A KELVIN SIGN → 'k' must not smuggle a
    previously-invalid input in as a silently different slug (#139 review).
    Non-strings pass through so `validate_slug` raises its own error."""
    if isinstance(slug, str) and slug.isascii():
        return slug.strip().lower()
    return slug


def validate_slug(slug: str) -> str:
    """Canonicalize (#134) then validate against §2.1; returns the CANONICAL
    slug — write seams assign the return value, so canonicalize-then-validate
    is structural rather than hand-paired at each call site."""
    slug = canonical_slug(slug)
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
    Input is canonicalized first (#134), so mixed-case GitHub-style slugs
    resolve; storage is canonical lowercase. No implicit stub creation in the
    public read path. This is THE shared seam: the /scopes routes and every
    plugin resolving through the scope API inherit case-insensitive input."""
    return session.scalar(select(Scope).where(Scope.slug == canonical_slug(slug)))


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
    org = canonical_slug(org)
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
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
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
    a bad slug/kind/parent. The `resolve()` pre-check below is check-then-act,
    not atomic — two concurrent creates for the SAME slug (a local create
    racing a replicated one, or two peers minting the same slug during a
    partition) can both pass it before either commits. The DB's unique
    constraint on `Scope.slug` is the real arbiter for that race: a flush
    collision is caught below and converted into the SAME `ScopeConflictError`,
    so `apply_scope_event`'s `ParkNow` fast path (#92) fires on the actual
    collision attempt rather than only on a later, self-healing retry.
    """
    slug = validate_slug(slug)
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
        parent = validate_slug(parent)
        prow = resolve(session, parent)
        if prow is None:
            raise ScopeNotFoundError(f"parent scope {parent!r} does not exist")
        parent_id = prow.id
    # else: parent is explicitly None/"" — parent_id stays None, NO derivation.

    # Explicit UTC stamps (§7.1's scope LWW clock — see `_utcnow`). The apply
    # seam passes the AUTHOR's stamps so a replicated row carries the origin's
    # coordinate, never import time.
    now = _utcnow()
    kwargs = dict(
        slug=slug,
        name=name,
        kind=kind,
        parent_id=parent_id,
        isolated=isolated,
        status=status,
        created_at=created_at or now,
        updated_at=updated_at or created_at or now,
    )
    if scope_id is not None:
        kwargs["id"] = scope_id
    scope = Scope(**kwargs)
    session.add(scope)
    try:
        session.flush()
    except IntegrityError as exc:
        # The race the resolve() pre-check above can't close (see docstring):
        # the unique constraint on `slug` is the atomic arbiter. Roll back the
        # failed insert and surface the SAME error the pre-check raises.
        session.rollback()
        raise ScopeConflictError(f"scope {slug!r} already exists") from exc
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
            parent = validate_slug(parent)
            prow = resolve(session, parent)
            if prow is None:
                raise ScopeNotFoundError(
                    f"parent scope {parent!r} does not exist"
                )
            scope.parent_id = prow.id

    # The LWW coordinate (§7.1): set EXPLICITLY (which also suppresses the
    # column's `onupdate=now()` default) and made strictly monotonic against the
    # row's current stamp — a local author SAW the value it overwrites, so its
    # write must beat it even when that value carries a peer's skewed-ahead
    # clock (last-writer means last-OBSERVED-writer; governance's emit clamp is
    # the same discipline).
    now = _utcnow()
    if scope.updated_at is not None and now <= scope.updated_at:
        now = scope.updated_at + timedelta(microseconds=1)
    scope.updated_at = now
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
    machinery.

    `created_at` / `updated_at` (§7.1, ADDITIVE — no contract bump; every apply
    reads them `.get`-tolerantly) are the scope's LWW clock: `updated_at`
    arbitrates metadata between two rows for the same slug (falling back to
    `created_at` when absent); `created_at` is provenance only."""
    return {
        "id": str(scope.id),
        "slug": scope.slug,
        "name": scope.name,
        "kind": scope.kind,
        "parent": scope.parent.slug if scope.parent is not None else None,
        "isolated": scope.isolated,
        "status": scope.status,
        "created_at": scope.created_at.isoformat() if scope.created_at else None,
        "updated_at": scope.updated_at.isoformat() if scope.updated_at else None,
    }


# --- §7.1 scope identity: slug is the cross-instance identity ----------------
#
# Live replication lets every instance hold the same scope UUID (one mint,
# replicated). A cold-start union breaks that: an instance stood up empty
# re-creates the same slugs under DIFFERENT ids, permanently. So scope UUIDs
# are instance-local and the SLUG is the identity: `scope.updated` applies by
# slug everywhere (wire id advisory), and on a backfill stream `scope.created`
# for a slug held under another id CONVERGES instead of parking.


def _metadata(name, kind, isolated, status, parent) -> str:
    """A canonical rendering of a scope's replicated metadata — the stable,
    content-derived tiebreak when two rows' LWW stamps are EQUAL (a pure
    function of the two states, so both sides pick the same winner)."""
    return json.dumps(
        [name, kind, bool(isolated), status, parent or None], sort_keys=True
    )


def _incoming_wins(local: Scope, payload: dict, incoming_at: datetime | None) -> bool:
    """§7.1's scope LWW: the incoming state wins iff its `(updated stamp,
    canonical metadata)` strictly beats the local row's. A payload with no
    stamp at all (a pre-§7.1 peer) applies unconditionally — today's live
    behavior, kept for rolling-upgrade compatibility."""
    if incoming_at is None:
        return True
    local_at = local.updated_at or local.created_at or datetime.min
    incoming_key = (
        incoming_at,
        _metadata(
            payload.get("name"), payload.get("kind"), payload.get("isolated"),
            payload.get("status"), payload.get("parent"),
        ),
    )
    local_key = (
        local_at,
        _metadata(
            local.name, local.kind, local.isolated, local.status,
            local.parent.slug if local.parent is not None else None,
        ),
    )
    return incoming_key > local_key


def _resolve_parent_id(session: Session, parent_slug: str | None) -> uuid.UUID | None:
    """The LOCAL row id for a payload's parent slug. An unknown parent is the
    ordinary retryable ordering gap (`ScopeNotFoundError`, §8) — never ParkNow."""
    if not parent_slug:
        return None
    prow = resolve(session, validate_slug(parent_slug))
    if prow is None:
        raise ScopeNotFoundError(f"parent scope {parent_slug!r} does not exist")
    return prow.id


def _converge(
    session: Session,
    local: Scope,
    payload: dict,
    *,
    heal_parent: bool,
) -> bool:
    """Converge `local` toward the incoming state by the §7.1 LWW clock. Returns
    whether the incoming state won.

    On a win, every metadata field takes the incoming value and the row's
    `updated_at` becomes the WINNING EVENT'S stamp — set explicitly, so the
    column's `onupdate=now()` cannot silently rebase the coordinate to import
    time (which would let an older backlogged update beat a newer one).

    `heal_parent` (the backfill `scope.created` convergence): the parent edge is
    resolved BY SLUG and a missing link heals from either side — the result is
    the winner's parent, or the loser's when the winner has none. A cold-start
    side that created `a/b/c` before `a/b` existed thus adopts the peer's
    `a/b` link (and keeps its own when it is the side holding the link), so
    both instances' ancestor chains stay congruent for §6.1's walk. A live
    `scope.updated` applies the winner's parent verbatim (an explicit clear is
    a real write)."""
    incoming_at = _parse_stamp(payload.get("updated_at")) or _parse_stamp(
        payload.get("created_at")
    )
    local_parent_slug = local.parent.slug if local.parent is not None else None
    wins = _incoming_wins(local, payload, incoming_at)
    if wins:
        parent_slug = payload.get("parent")
        if heal_parent and not parent_slug:
            parent_slug = local_parent_slug
        if (payload.get("kind") or local.kind) == "org":
            parent_slug = None
        kind = payload.get("kind") or local.kind
        _validate_kind_for_slug(local.slug, kind)
        local.parent_id = _resolve_parent_id(session, parent_slug)
        local.name = payload.get("name", local.name)
        local.kind = kind
        local.isolated = bool(payload.get("isolated", local.isolated))
        local.status = payload.get("status", local.status)
        local.updated_at = incoming_at or _utcnow()
    elif heal_parent and local_parent_slug is None and payload.get("parent"):
        if local.kind != "org":
            # A heal is convergence, not a new write: the LWW coordinate stays.
            local.parent_id = _resolve_parent_id(session, payload["parent"])
    # Force `updated_at` into the UPDATE's SET clause even when its value is
    # unchanged (a heal, or a win at an equal stamp): left out, the column's
    # `onupdate=now()` default would silently rebase the coordinate to import
    # time on any row this apply touched.
    flag_modified(local, "updated_at")
    session.flush()
    session.expire(local, ["parent"])
    return wins


def apply_scope_event(session: Session, envelope: dict) -> None:
    """The platform's replication APPLY function (spec §8) — the seam
    `replication.py` wires into the SDK's `ingest_delivery`
    (`snowline_plugin_sdk.replication.ingest`). Runs under origin suppression,
    so `create`/`update`'s own `emit_event` call is a no-op here (§3.2 hard
    rule) — replicated writes can never boomerang back onto the wire.

    Reusing `create`/`update` rather than a separate write path means their
    EXISTING exceptions ARE the §8 error taxonomy — the two cases split by
    whether waiting can ever help (issue #92):

      * `ScopeConflictError` (the slug is already taken by a DIFFERENT id) —
        the cross-partition slug collision spec §8 calls out by name. It will
        NEVER resolve itself by waiting (the colliding slug does not stop
        colliding; the local id differs from the event's, so it is not an
        idempotent replay), so this apply re-raises it as the SDK's `ParkNow`
        (#92): the event parks on THIS delivery with no retry budget spent.
        Parking already IS spec §8's "fail loud, manual resolution" — first-
        class state (tool/UI/health signal, never a log line) that stays
        re-appliable via `reapply_parked` once the slug is freed. Freeing it
        today means MANUAL DB SURGERY on the losing local row: slugs are
        immutable by design (spec §2 — no rename path), and retiring via
        `status` does not free the slug (`resolve` matches regardless of
        status), so an operator must delete the losing row (or re-slug it by
        hand) directly; purpose-built remediation tooling is TBD. A `ParkNow`
        park is bit-for-bit the same state as a bound-reached park — it only
        skips the pointless backoff budget before the inevitable park (the
        friction #92 filed from this exact call site).
      * `ScopeNotFoundError` (the parent slug hasn't replicated yet) — an
        ordering gap, not a permanent failure (spec §8's ordering note): it
        stays on the DEFAULT bounded-retryable path (NOT `ParkNow`) and
        self-heals the moment the parent's own `scope.created` applies, so
        ordinary scope-stream lag never drops data. This is precisely the
        transient case the retry bound exists for.

    Idempotent past the gate (checklist item 4, §4): a payload `id` already
    present locally is a no-op — covers `reapply_parked` replay and any
    future re-delivery ambiguity, even though the SDK's watermark already
    keeps a live stream from re-invoking apply for an already-applied seq.

    **§7.1 narrows the park posture** to the LIVE `scope.created` same-new-slug
    race above. `scope.updated` applies BY SLUG everywhere (wire id advisory)
    and converges metadata by the payload's `updated_at` LWW clock — an id
    mismatch is no longer a collision, because a cold-start union leaves scope
    ids permanently instance-local. On a BACKFILL stream (`#backfill` source),
    `scope.created` for a slug held locally converges by slug (`_converge`,
    parent healed by slug) instead of parking — the collision is the expected
    case for every shared scope there, not a race.
    """
    payload = envelope["payload"]
    event_type = envelope["event_type"]
    scope_id = uuid.UUID(payload["id"])
    backfill = is_backfill_envelope(envelope)

    if event_type == EVENT_SCOPE_CREATED:
        if backfill:
            # §7.1 union convergence: slug is the identity. A slug held locally
            # (under ANY id — the peer's, from a shared pre-divergence era, or
            # this instance's own re-creation) is the EXPECTED case for every
            # shared scope, not a race: keep the local row and id, LWW the
            # metadata, heal the parent edge by slug, log at WARNING — and
            # never park (parking would flood §8.1 with non-events and pin the
            # scope stream's frontier at zero).
            local = resolve(session, payload["slug"])
            if local is None and session.get(Scope, scope_id) is not None:
                local = session.get(Scope, scope_id)
            if local is not None:
                won = _converge(session, local, payload, heal_parent=True)
                if local.id != scope_id:
                    log.warning(
                        "scope %r converged by slug on a union backfill: local id "
                        "%s kept, peer id %s advisory (metadata %s, spec §7.1)",
                        local.slug, local.id, scope_id,
                        "taken from the peer" if won else "kept",
                    )
                return
        elif session.get(Scope, scope_id) is not None:
            return  # already applied — idempotent replay
        try:
            create(
                session,
                slug=payload["slug"],
                name=payload["name"],
                kind=payload["kind"],
                parent=payload["parent"],
                isolated=payload["isolated"],
                status=payload["status"],
                scope_id=scope_id,
                created_at=_parse_stamp(payload.get("created_at")),
                updated_at=_parse_stamp(payload.get("updated_at")),
            )
        except ScopeConflictError as exc:
            # A LIVE same-new-slug race across a partition (§8, narrowed by
            # §7.1 to exactly this case): the slug is taken by a different id
            # and will never un-collide — park now, don't burn the retry budget
            # (#92). ScopeNotFoundError (unknown parent) is NOT caught here: it
            # stays retryable and self-heals.
            raise ParkNow(str(exc)) from exc
    elif event_type == EVENT_SCOPE_UPDATED:
        # §7.1: `scope.updated` applies BY SLUG everywhere; the wire id is
        # advisory. After a union, scope ids are permanently instance-local, and
        # an id-keyed update would park every post-union update to a shared
        # slug forever (no re-apply could ever succeed). Under live-from-genesis
        # replication the ids coincide, so nothing observable changes there.
        existing = resolve(session, payload["slug"])
        if existing is None:
            # The scope hasn't replicated here yet — the ordinary retryable
            # ordering gap (§8), never a park.
            raise ScopeNotFoundError(f"no scope with slug {payload['slug']!r}")
        if existing.id != scope_id:
            log.info(
                "scope.updated for %r applied by slug: wire id %s, local id %s "
                "(spec §7.1)", existing.slug, scope_id, existing.id,
            )
        _converge(session, existing, payload, heal_parent=False)
    else:
        raise ValueError(
            f"scope replication apply: unknown event_type {event_type!r}"
        )


def enumerate_backfill(session: Session):
    """The platform's §7.1 scope enumerator — `scope.created` ONLY (never
    `scope.updated`), one per row, carrying the row's FINAL state including its
    authored/updated stamps: on an empty peer it reproduces the row; on a peer
    holding the slug it is the backfill convergence path (`_converge`).

    Walk order is deterministic AND parent-first: rows sorted by `(created_at,
    id)`, each emitted only after its parent (a topological pass over the
    `parent_id` forest), so a child's parent slug always resolves on the
    receiver within the same stream — no ordering-gap retries. Read-only.

    Milestones ride the same platform stream and are enumerated AFTER the
    scopes by `replication.enumerate_backfill` (`milestones.enumerate_backfill`),
    so every anchor is on the receiver before its milestones arrive."""
    rows = list(session.scalars(select(Scope).order_by(Scope.created_at, Scope.id)))
    by_id = {r.id: r for r in rows}
    emitted: set[uuid.UUID] = set()

    def visit(row: Scope, stack: set[uuid.UUID]):
        if row.id in emitted:
            return
        parent = by_id.get(row.parent_id) if row.parent_id else None
        if parent is not None and parent.id not in stack:
            yield from visit(parent, stack | {parent.id})
        emitted.add(row.id)
        yield (EVENT_SCOPE_CREATED, to_replication_payload(row))

    for row in rows:
        yield from visit(row, {row.id})
