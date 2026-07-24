"""The milestone service — the platform-owned operations over the milestone
registry (milestones.md §3/§4). The platform's SECOND identity primitive, built
in the SAME shape as the scope service (`scopes.py`): a read/resolve + create +
lifecycle surface that the HTTP API wraps, keyed on the canonical address
`(anchor slug, name)`.

Resolution is the drift killer (§3): shorthand is a legitimate INPUT format
(humans speak it, agents relay it) so the service resolves it, but STORAGE is
always canonical. A 2- or 3-segment address resolves directly; a BARE name
requires a context and walks it repo-then-org, most-specific-first; a bare name
with NO context always hard-fails (listing candidates, even a unique one — the
uniform-strictness rule); an unknown ref hard-fails with near-miss suggestions;
NOTHING ever auto-creates (mirrors the scope `resolve` posture).

Case-insensitive input composes with scope-slug folding (#134/#139): the anchor
part folds through the scope grammar (`scopes.validate_slug`), the slash-free
name folds the same ASCII-only way (`validate_name`); everything stores canonical
lowercase.

Increment 1 (spec §5 first-cut note): model + service (create / resolve / get /
list / lifecycle / update) + the HTTP read/resolve API + the MCP tool wrappers
(served on the platform `main` surface via the platform registering itself as an
upstream, decision 0503fff0; `platform_tools.py`).

Increment 2 (§7 + the dependency parts of §2/§4): the `merge` verb (alias
tombstones, terminal-target resolution keeping chains depth-1, state
compatibility, dependency-edge re-pointing with a re-run cycle guard) — which
ACTIVATES `_follow_alias` on the resolution path — plus the `aliases` closure
read and the dependency edge verbs (`add_dependency` / `remove_dependency` /
`dependencies`, cycle-guarded over the global edge set). DEFERRED to later
increments: replication emission (§9), the governance consumer (§6.1), and the
`empty_rounds`-style health signal.
"""

from __future__ import annotations

import difflib
import os
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from snowline_platform import scopes
from snowline_platform.models import (
    Milestone,
    MilestoneDependency,
    MilestoneTransition,
    MilestoneUnreconciled,
)
from snowline_platform.scopes import canonical_slug, validate_slug
from snowline_plugin_sdk.contract import (
    EVENT_MILESTONE_CREATED,
    EVENT_MILESTONE_DEPENDENCY_CHANGED,
    EVENT_MILESTONE_MERGED,
    EVENT_MILESTONE_TRANSITIONED,
    EVENT_MILESTONE_UPDATED,
)
from snowline_plugin_sdk.replication import ParkNow, emit_event

# --- name / status contract (§2) --------------------------------------------

# A milestone `name` is a SLASH-FREE slug — a single scope-style segment (the
# GitHub identifier set lowercased). Reusing the scope segment shape keeps the
# folding rules identical; the absence of `/` in the charset is exactly what
# makes the address grammar self-describing by segment count (§2).
_NAME_SEG = r"[._-]*[a-z0-9][a-z0-9._-]*"
NAME_RE = re.compile(rf"^{_NAME_SEG}$")

# Names that collide with the HTTP surface's ADDRESS-SUFFIX routes
# (`/{address}/transitions|aliases|dependencies|activate|achieve|cancel`): a
# milestone so named would make requests to its own address misroute to the
# suffix handler with a SHORTER address — the exact class of grammar ambiguity
# the slash-free name rule exists to kill — so they are reserved at the name
# level and can never exist. (The fixed single-segment paths — `resolve`,
# `resolve-batch`, `merge` — cannot collide: an address is always ≥2 segments.)
RESERVED_NAMES = frozenset(
    {"transitions", "aliases", "dependencies", "activate", "achieve", "cancel"}
)

# The lifecycle status set and the LEGAL transition table (§4). Legal moves:
# planned→active→achieved; planned|active→cancelled. `achieve` on a *planned*
# milestone is rejected ("activate first") — never auto-activates. Terminal
# states (achieved, cancelled) admit no further transition.
VALID_STATUSES = frozenset({"planned", "active", "achieved", "cancelled"})


class InvalidMilestoneNameError(ValueError):
    """Name violates the §2 slash-free-slug convention."""


class InvalidAnchorError(ValueError):
    """The anchor is not a valid milestone anchor — either not a 1-or-2-segment
    scope slug (§2), or not a registered scope."""


class InvalidMilestoneFieldError(ValueError):
    """A field value is not valid for a milestone."""


class MilestoneNotFoundError(LookupError):
    """No milestone at the given address."""


class MilestoneConflictError(ValueError):
    """A milestone with the given (anchor, name) already exists — including a
    merge tombstone, which reserves the name forever (§2/§4)."""


class IllegalTransitionError(ValueError):
    """A lifecycle verb was applied from an illegal source status (§4)."""


class MilestoneMergeError(ValueError):
    """A merge is illegal (§7): the terminal target equals `from` (cycle guard),
    the two states are not merge-compatible, or the dependency-edge union that the
    merge would produce cycles. No partial write ever lands."""


class MilestoneDependencyError(ValueError):
    """A dependency edge is illegal (§2/§4): a self-edge, or (as
    `DependencyCycleError`) an edge that would cycle the global DAG."""


class DependencyCycleError(MilestoneDependencyError):
    """Adding the edge would create a cycle in the global dependency DAG (§2)."""


class MilestoneResolutionError(LookupError):
    """A ref could not be resolved (§3). Carries `suggestions` — near-miss or
    same-named candidates surfaced for the agent, NEVER an automatic resolution
    (bare names never resolve outside the walk; unknown never mints)."""

    def __init__(self, message: str, suggestions: list[dict] | None = None) -> None:
        super().__init__(message)
        self.suggestions = suggestions or []


# A sentinel distinguishing "not provided" from an explicit value (including
# explicit None) on `update` — same device the scope service uses.
_UNSET = object()


def _now() -> datetime:
    """A naive-UTC timestamp for the lifecycle `*_at` columns (TIMESTAMP WITHOUT
    TIME ZONE, matching the scope table's convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# The LEGAL lifecycle moves (§4), as the (from, to) pairs the transition verbs
# permit: planned→active→achieved; planned|active→cancelled. This is the table
# the replication illegal-history check (§9) reconstructs the CONVERGED transition
# path against — a converged history whose effective path steps outside this set
# (e.g. cancelled→active, a terminal state reversed by an LWW-winning transition
# authored during a partition) is first-class unreconciled state, not a park.
LEGAL_TRANSITIONS = frozenset(
    {
        ("planned", "active"),
        ("active", "achieved"),
        ("planned", "cancelled"),
        ("active", "cancelled"),
    }
)


def _local_source_id() -> str | None:
    """This instance's replication `source_id` (`SNOWLINE_REPLICATION_SOURCE_ID`),
    or None when replication is unconfigured. Stamped onto locally-authored rows /
    transitions as the §6 LWW identity — it equals the `source` an outbound
    subscription stamps into this instance's envelopes (both default from the same
    env var), so a peer's apply compares like-for-like. None when unset: no
    outbound stream exists to emit into anyway, so the clock is inert."""
    return os.environ.get("SNOWLINE_REPLICATION_SOURCE_ID") or None


# --- name / address helpers -------------------------------------------------


def canonical_name(name: str):
    """Fold a milestone name to canonical form — the SAME ASCII-only lowercase +
    strip as scope slugs (`scopes.canonical_slug`), so mixed-case input folds and
    non-ASCII passes through to fail the grammar loudly (#139)."""
    return canonical_slug(name)


def validate_name(name: str) -> str:
    """Canonicalize then validate a milestone name against §2; returns the
    CANONICAL name. A name containing `/` fails (slash is not in the segment
    charset) — that is what keeps the address grammar unambiguous."""
    folded = canonical_name(name)
    if not isinstance(folded, str) or not NAME_RE.match(folded):
        raise InvalidMilestoneNameError(
            f"invalid milestone name: {name!r} — must be a slash-free lowercase "
            "slug (§2)"
        )
    if folded in RESERVED_NAMES:
        raise InvalidMilestoneNameError(
            f"milestone name {folded!r} is reserved — it collides with the "
            "address-suffix route grammar "
            f"(/{{address}}/{folded} is an operation, not a milestone)"
        )
    return folded


def address_of(milestone: Milestone) -> str:
    """The canonical address `<anchor slug>/<name>` — the cross-instance identity
    (§9) and the shape every consumer stores."""
    return f"{milestone.anchor.slug}/{milestone.name}"


# --- internal lookups -------------------------------------------------------


def _by_anchor_name(
    session: Session, anchor_scope_id: uuid.UUID, name: str
) -> Milestone | None:
    return session.scalar(
        select(Milestone).where(
            Milestone.anchor_scope_id == anchor_scope_id,
            Milestone.name == name,
        )
    )


def _live(session: Session) -> list[Milestone]:
    """Every non-tombstone milestone (merge deferred, so this is all of them)."""
    return list(
        session.scalars(
            select(Milestone).where(Milestone.merged_into_id.is_(None))
        )
    )


def _same_named(session: Session, name: str) -> list[dict]:
    """Same-named live milestones across ALL anchors — the candidate set a
    bare-name walk miss / no-context failure surfaces as SUGGESTIONS only (§3)."""
    return [
        {"address": address_of(m), "status": m.status}
        for m in _live(session)
        if m.name == name
    ]


def _near_misses(session: Session, name: str, ref: str) -> list[dict]:
    """Near-miss candidates for an unknown ref (§3 typo case). Matches on the
    bare name AND the full address so both `v1-lanch` (name typo) and a mistyped
    anchor surface a suggestion; de-duplicated, order-stable."""
    live = _live(session)
    names = {m.name for m in live}
    addrs = {address_of(m) for m in live}
    hits: list[str] = difflib.get_close_matches(name, list(names), n=3, cutoff=0.6)
    hits += difflib.get_close_matches(ref, list(addrs), n=3, cutoff=0.6)
    out: list[dict] = []
    seen: set[str] = set()
    for m in live:
        if (m.name in hits or address_of(m) in hits) and address_of(m) not in seen:
            seen.add(address_of(m))
            out.append({"address": address_of(m), "status": m.status})
    return out


def _suggestion_tail(suggestions: list[dict]) -> str:
    if not suggestions:
        return ""
    shown = ", ".join(f"{s['address']} ({s['status']})" for s in suggestions[:3])
    return f" — did you mean {shown}?"


# --- resolution (§3) --------------------------------------------------------


def _normalize_context(context_slug: str) -> list[str]:
    """The ordered anchor slugs a bare-name walk visits, most-specific-first
    (§3). The context is first normalized to its nearest ancestor-or-self
    REPO-level (2-segment) scope:

      * an ORG context (1 segment) skips the repo step and walks org ONLY;
      * a REPO context (2 segments) is its own repo level;
      * an INITIATIVE / component context (3+ segments) normalizes UP to its
        repo (the first two segments),

    then the walk tries that repo anchor, then its org anchor — repo shadows org,
    predictably. Normalization is LEXICAL over the canonical slug (the slug
    hierarchy is kept consistent with `parent_id` on create), so a context that
    is not itself a registered scope still normalizes."""
    segs = context_slug.split("/")
    if len(segs) == 1:
        return [segs[0]]
    return ["/".join(segs[:2]), segs[0]]


def _follow_alias(session: Session, m: Milestone) -> tuple[Milestone, bool]:
    """Follow a merge tombstone to its terminal target (§3). Tombstones store the
    terminal target directly (`merge` resolves `into` to its terminal target
    first), so chains stay depth-1 and a single hop suffices; a non-tombstone
    resolves to itself with `resolved_via_alias=False`."""
    if m.merged_into_id is None:
        return m, False
    target = session.get(Milestone, m.merged_into_id)
    if target is None:  # pragma: no cover - FK guarantees presence
        raise MilestoneResolutionError(
            f"milestone {address_of(m)!r} is a tombstone with a dangling target"
        )
    return target, True


def resolve_row(
    session: Session, ref: str, context: str | None = None
) -> tuple[Milestone, bool]:
    """Resolve `ref` to `(milestone, resolved_via_alias)` (§3), following a merge
    tombstone to its terminal target. Raises `MilestoneResolutionError` (carrying
    `suggestions`) on any miss — nothing is ever minted. See `resolve` for the
    convenience that returns just the milestone."""
    folded = canonical_slug(ref)
    if not isinstance(folded, str) or not folded:
        raise MilestoneResolutionError(f"empty milestone reference: {ref!r}")
    segs = folded.split("/")

    if len(segs) == 1:
        return _resolve_bare(session, validate_name(segs[0]), context, ref)
    if len(segs) in (2, 3):
        anchor_slug = validate_slug("/".join(segs[:-1]))
        name = validate_name(segs[-1])
        return _resolve_direct(session, anchor_slug, name, ref)
    raise MilestoneResolutionError(
        f"malformed milestone address {ref!r} — at most <org>/<repo>/<name> "
        "(3 segments; the name is slash-free)"
    )


def resolve(session: Session, ref: str, context: str | None = None) -> Milestone:
    """Resolve `ref` to its terminal milestone (§3), following any merge alias.
    Raises `MilestoneResolutionError` on a miss."""
    return resolve_row(session, ref, context)[0]


def _resolve_direct(
    session: Session, anchor_slug: str, name: str, ref: str
) -> tuple[Milestone, bool]:
    """A 2-/3-segment address resolves DIRECTLY against its stated anchor (§3).
    A miss is unknown — hard-fail with near-miss suggestions; never mint."""
    anchor = scopes.resolve(session, anchor_slug)
    m = _by_anchor_name(session, anchor.id, name) if anchor is not None else None
    if m is None:
        sugg = _near_misses(session, name, f"{anchor_slug}/{name}")
        raise MilestoneResolutionError(
            f"unknown milestone {anchor_slug + '/' + name!r}"
            + _suggestion_tail(sugg),
            suggestions=sugg,
        )
    return _follow_alias(session, m)


def _resolve_bare(
    session: Session, name: str, context: str | None, ref: str
) -> tuple[Milestone, bool]:
    """A bare name REQUIRES a context and walks it (§3). No context always
    hard-fails listing candidates — even a unique one — so context can never make
    resolution STRICTER than omitting it (the uniform-strictness rule)."""
    if context is None or (isinstance(context, str) and not context.strip()):
        cands = _same_named(session, name)
        raise MilestoneResolutionError(
            f"bare milestone name {name!r} needs a context to resolve"
            + _suggestion_tail(cands),
            suggestions=cands,
        )
    walk = _normalize_context(canonical_slug(context))
    for anchor_slug in walk:
        anchor = scopes.resolve(session, anchor_slug)
        if anchor is None:
            continue
        m = _by_anchor_name(session, anchor.id, name)
        if m is not None:
            return _follow_alias(session, m)
    # Walk miss: same-named milestones at OTHER anchors are suggestions only —
    # a bare name never resolves outside the walk (§3).
    cands = _same_named(session, name) or _near_misses(session, name, ref)
    raise MilestoneResolutionError(
        f"unknown milestone {name!r} in context {context!r}"
        + _suggestion_tail(cands),
        suggestions=cands,
    )


# --- read -------------------------------------------------------------------


def get(session: Session, address: str) -> Milestone:
    """The row at `address` (§4), by its DIRECT 2-/3-segment address — NOT
    following a merge alias (that is `resolve`'s job; `get` is the audit read that
    returns a tombstone as itself). Raises `MilestoneNotFoundError` if unknown."""
    folded = canonical_slug(address)
    if not isinstance(folded, str):
        raise MilestoneNotFoundError(f"invalid milestone address: {address!r}")
    segs = folded.split("/")
    if len(segs) not in (2, 3):
        raise MilestoneNotFoundError(
            f"invalid milestone address {address!r} — expected "
            "<org>/<name> or <org>/<repo>/<name>"
        )
    # A grammar-invalid address addresses NOTHING — surface it as not-found, so
    # every caller of `get` (the lifecycle verbs, update, transitions, and their
    # HTTP routes) fails 404-clean instead of leaking a validation error.
    try:
        anchor_slug = validate_slug("/".join(segs[:-1]))
        name = validate_name(segs[-1])
    except (scopes.InvalidSlugError, InvalidMilestoneNameError) as exc:
        raise MilestoneNotFoundError(
            f"invalid milestone address {address!r}: {exc}"
        ) from exc
    anchor = scopes.resolve(session, anchor_slug)
    m = _by_anchor_name(session, anchor.id, name) if anchor is not None else None
    if m is None:
        raise MilestoneNotFoundError(f"no milestone at address {address!r}")
    return m


def list_milestones(
    session: Session,
    anchor: str | None = None,
    status: str | None = None,
    include_merged: bool = False,
) -> list[dict]:
    """Registry rows, address-ordered (§4). `anchor` SUBTREE-filters — the given
    anchor scope and everything below it (slug-prefix), so listing an org anchor
    surfaces its repo-anchored milestones too. `status` filters by lifecycle
    status. Tombstones are EXCLUDED by default (`include_merged=` opts in)."""
    anchor_slug = canonical_slug(anchor) if anchor is not None else None
    rows: list[tuple[str, Milestone]] = []
    for m in session.scalars(select(Milestone)):
        if not include_merged and m.merged_into_id is not None:
            continue
        if status is not None and m.status != status:
            continue
        a = m.anchor.slug
        if anchor_slug is not None and not (
            a == anchor_slug or a.startswith(anchor_slug + "/")
        ):
            continue
        rows.append((address_of(m), m))
    rows.sort(key=lambda r: r[0])
    return [to_row(m) for _, m in rows]


# --- create -----------------------------------------------------------------


def create(
    session: Session,
    anchor: str,
    name: str,
    outcome: str | None = None,
    target_date: date | None = None,
) -> Milestone:
    """The ONLY mint path (§4). Enforces a slash-free name, a 1-or-2-segment
    REGISTERED anchor scope, and uniqueness against live rows AND tombstones alike
    (a tombstoned name is reserved forever; the error names the alias target).
    Every milestone is born `planned` — lifecycle is explicit verbs, never
    automatic (§2/§4). Raises `MilestoneConflictError` on a duplicate (including a
    case-only duplicate, since input folds first), `InvalidAnchorError` /
    `InvalidMilestoneNameError` on a bad anchor / name."""
    anchor_slug = validate_slug(anchor)
    if len(anchor_slug.split("/")) not in (1, 2):
        raise InvalidAnchorError(
            f"milestone anchor {anchor!r} must be a 1-or-2-segment scope "
            "(org- or repo-level); no portfolio/global anchor (§2)"
        )
    anchor_scope = scopes.resolve(session, anchor_slug)
    if anchor_scope is None:
        raise InvalidAnchorError(
            f"anchor scope {anchor_slug!r} is not registered — create it on the "
            "platform first (nothing auto-vivifies)"
        )
    name = validate_name(name)

    existing = _by_anchor_name(session, anchor_scope.id, name)
    if existing is not None:
        if existing.merged_into_id is not None:
            raise MilestoneConflictError(
                f"milestone {anchor_slug + '/' + name!r} is a merge tombstone "
                f"aliased to {address_of(existing.merged_into)!r} — the name is "
                "reserved (§4)"
            )
        raise MilestoneConflictError(
            f"milestone {anchor_slug + '/' + name!r} already exists"
        )

    m = Milestone(
        anchor_scope_id=anchor_scope.id,
        name=name,
        outcome=outcome,
        target_date=target_date,
        status="planned",
        lww_authored_at=_now(),
        lww_source_id=_local_source_id(),
    )
    session.add(m)
    try:
        session.flush()
    except IntegrityError as exc:
        # The (anchor, name) unique constraint is the atomic arbiter for a
        # check-then-act race, mirroring the scope service.
        session.rollback()
        raise MilestoneConflictError(
            f"milestone {anchor_slug + '/' + name!r} already exists"
        ) from exc
    emit_event(session, EVENT_MILESTONE_CREATED, to_replication_payload(m))
    return m


# --- lifecycle (§4) ---------------------------------------------------------


def _log_transition(
    session: Session,
    m: Milestone,
    from_status: str,
    to_status: str,
    reason: str | None,
    *,
    authored_at: datetime,
    source_id: str | None,
) -> None:
    session.add(
        MilestoneTransition(
            milestone_id=m.id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            authored_at=authored_at,
            source_id=source_id,
        )
    )


def _transition(
    session: Session, m: Milestone, to_status: str, stamp: str, reason: str | None
) -> Milestone:
    """Apply one lifecycle transition + stamp the §6 LWW clock + emit
    `milestone.transitioned` (§9). `stamp` is the `*_at` column to set. The
    transition's `authored_at` == the row's `lww_authored_at` == the emitted
    event's `authored_at`, all one instant, so a peer's LWW comparison and its
    illegal-history reconstruction see the identical clock this instance did."""
    frm = m.status
    now = _now()
    src = _local_source_id()
    m.status = to_status
    setattr(m, stamp, now)
    m.lww_authored_at = now
    m.lww_source_id = src
    _log_transition(session, m, frm, to_status, reason, authored_at=now, source_id=src)
    session.flush()
    emit_event(
        session,
        EVENT_MILESTONE_TRANSITIONED,
        _transition_payload(m, frm, to_status, reason, now),
    )
    return m


def activate(
    session: Session, address: str, reason: str | None = None
) -> Milestone:
    """planned→active (§4). Rejects any other source status; records the
    transition (with optional `reason`) and emits `milestone.transitioned` (§9).
    Nothing is ever automatic."""
    m = get(session, address)
    if m.status != "planned":
        raise IllegalTransitionError(
            f"cannot activate {address!r} from status {m.status!r} — "
            "only a planned milestone activates (§4)"
        )
    return _transition(session, m, "active", "activated_at", reason)


def achieve(
    session: Session, address: str, reason: str | None = None
) -> Milestone:
    """active→achieved (§4). `achieve` on a PLANNED milestone is REJECTED —
    "activate first"; it never auto-activates, and no member-item state ever
    implies achievement. Records the transition + emits `milestone.transitioned`."""
    m = get(session, address)
    if m.status == "planned":
        raise IllegalTransitionError(
            f"cannot achieve {address!r} — it is still planned; activate first "
            "(achievement is never automatic, §4)"
        )
    if m.status != "active":
        raise IllegalTransitionError(
            f"cannot achieve {address!r} from status {m.status!r} — "
            "only an active milestone is achieved (§4)"
        )
    return _transition(session, m, "achieved", "achieved_at", reason)


def cancel(
    session: Session, address: str, reason: str | None = None
) -> Milestone:
    """planned|active→cancelled (§4) — a deliberate retraction. Records the
    transition + emits `milestone.transitioned`. (Cancelling an ACTIVE milestone
    demotes governance versions stamped with it, §6.1.6 — that warning surfaces
    once the governance consumer lands; deferred here.)"""
    m = get(session, address)
    if m.status not in ("planned", "active"):
        raise IllegalTransitionError(
            f"cannot cancel {address!r} from status {m.status!r} — "
            "only a planned or active milestone is cancelled (§4)"
        )
    return _transition(session, m, "cancelled", "cancelled_at", reason)


def update(
    session: Session,
    address: str,
    *,
    outcome=_UNSET,
    target_date=_UNSET,
) -> Milestone:
    """Modify display fields — `outcome` / `target_date` — NEVER identity (§4).
    A provided value of `None` CLEARS the field; omitting the argument leaves it
    unchanged. A REAL change stamps the §6 LWW clock + emits `milestone.updated`
    (§9); a no-op call (nothing provided, or values equal to what is stored)
    stamps and emits NOTHING — a content-free write must not advance the LWW
    clock, or it could shadow a genuine concurrent peer update. Raises
    `MilestoneNotFoundError` if unknown."""
    m = get(session, address)
    changed = False
    if outcome is not _UNSET and m.outcome != outcome:
        m.outcome = outcome
        changed = True
    if target_date is not _UNSET and m.target_date != target_date:
        m.target_date = target_date
        changed = True
    if changed:
        m.lww_authored_at = _now()
        m.lww_source_id = _local_source_id()
        session.flush()
        emit_event(session, EVENT_MILESTONE_UPDATED, to_replication_payload(m))
    return m


# --- transitions read (audit) -----------------------------------------------


def transitions(session: Session, address: str) -> list[dict]:
    """The append-only transition log for a milestone, oldest-first (§2)."""
    m = get(session, address)
    rows = session.scalars(
        select(MilestoneTransition)
        .where(MilestoneTransition.milestone_id == m.id)
        .order_by(MilestoneTransition.authored_at.asc())
    )
    return [
        {
            "from_status": t.from_status,
            "to_status": t.to_status,
            "reason": t.reason,
            "authored_at": _iso(t.authored_at),
        }
        for t in rows
    ]


# --- dependencies (§2/§4) ---------------------------------------------------


def _all_edges(session: Session) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """Every dependency edge as `(dependent_id, dependency_id)` tuples — the GLOBAL
    edge set the cycle guard reasons over (cross-anchor edges included; the anchor
    is not a fence, §2)."""
    return {
        (e.dependent_id, e.dependency_id)
        for e in session.scalars(select(MilestoneDependency))
    }


def _has_cycle(edges: set[tuple[uuid.UUID, uuid.UUID]]) -> bool:
    """Does the directed graph of `dependent → dependency` edges contain a cycle?
    A dependency DAG must stay acyclic so §3's alias traversal and the dependency
    walk are loop-free by construction (§9). Standard three-colour DFS."""
    adj: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for dependent, dependency in edges:
        adj[dependent].add(dependency)
    colour: dict[uuid.UUID, int] = {}  # 1 = on stack, 2 = done

    def visit(node: uuid.UUID) -> bool:
        colour[node] = 1
        for nxt in adj[node]:
            c = colour.get(nxt, 0)
            if c == 1 or (c == 0 and visit(nxt)):
                return True
        colour[node] = 2
        return False

    return any(colour.get(n, 0) == 0 and visit(n) for n in list(adj))


def _edge(
    session: Session, dependent_id: uuid.UUID, dependency_id: uuid.UUID
) -> MilestoneDependency | None:
    return session.scalar(
        select(MilestoneDependency).where(
            MilestoneDependency.dependent_id == dependent_id,
            MilestoneDependency.dependency_id == dependency_id,
        )
    )


def add_dependency(
    session: Session, dependent_address: str, dependency_address: str
) -> dict:
    """Add a `dependent → dependency` edge (§2/§4): *dependent* depends on
    *dependency*. Both refs resolve through any merge alias to their terminal
    target (a dependency stored via a tombstone points at the live target). A
    self-edge is rejected; a duplicate is IDEMPOTENT (re-adding an existing edge is
    a no-op success — documented over conflict so replay/retry is safe); the edge
    is cycle-guarded over the GLOBAL edge set (cross-anchor edges allowed) and
    rejected with `DependencyCycleError` if it would cycle. Returns the current
    `dependencies` read of `dependent`."""
    dependent = resolve(session, dependent_address)
    dependency = resolve(session, dependency_address)
    if dependent.id == dependency.id:
        raise MilestoneDependencyError(
            f"a milestone cannot depend on itself ({address_of(dependent)!r}, §2)"
        )
    if _edge(session, dependent.id, dependency.id) is None:
        edges = _all_edges(session)
        edges.add((dependent.id, dependency.id))
        if _has_cycle(edges):
            raise DependencyCycleError(
                f"{address_of(dependent)!r} depending on "
                f"{address_of(dependency)!r} would cycle the dependency DAG — "
                "rejected (the guard runs over the global edge set, §2)"
            )
        session.add(
            MilestoneDependency(
                dependent_id=dependent.id, dependency_id=dependency.id
            )
        )
        session.flush()
        emit_event(
            session,
            EVENT_MILESTONE_DEPENDENCY_CHANGED,
            _dependency_payload("add", dependent, dependency),
        )
    return _dependencies_of(session, dependent)


def remove_dependency(
    session: Session, dependent_address: str, dependency_address: str
) -> dict:
    """Remove a `dependent → dependency` edge (§4). Both refs resolve through any
    merge alias. IDEMPOTENT — removing an absent edge is a no-op success (matching
    `add_dependency`). Returns the current `dependencies` read of `dependent`."""
    dependent = resolve(session, dependent_address)
    dependency = resolve(session, dependency_address)
    edge = _edge(session, dependent.id, dependency.id)
    if edge is not None:
        session.delete(edge)
        session.flush()
        emit_event(
            session,
            EVENT_MILESTONE_DEPENDENCY_CHANGED,
            _dependency_payload("remove", dependent, dependency),
        )
    return _dependencies_of(session, dependent)


def _dep_row(m: Milestone) -> dict:
    """A dependency neighbour as `{address, status}` — the raw status is ALWAYS
    surfaced, including `cancelled`: a dependency on a cancelled milestone must be
    visible so PM's readiness read can flag `blocked_by_cancelled` (§4)."""
    return {"address": address_of(m), "status": m.status}


def _dependencies_of(session: Session, m: Milestone) -> dict:
    """Both directions of `m`'s dependency edges (§4): `depends_on` (what `m`
    depends on) and `dependents` (what depends on `m`), each address-ordered with
    raw status."""
    depends_on = [
        _dep_row(session.get(Milestone, dep_id))
        for (dpt, dep_id) in _all_edges(session)
        if dpt == m.id
    ]
    dependents = [
        _dep_row(session.get(Milestone, dpt_id))
        for (dpt_id, dep) in _all_edges(session)
        if dep == m.id
    ]
    return {
        "address": address_of(m),
        "depends_on": sorted(depends_on, key=lambda r: r["address"]),
        "dependents": sorted(dependents, key=lambda r: r["address"]),
    }


def dependencies(session: Session, address: str) -> dict:
    """Read both directions of a milestone's dependency edges (§4). The address
    resolves through any merge alias, so a dependency read via a tombstone reports
    the live target's edges. Raises on an unknown ref."""
    return _dependencies_of(session, resolve(session, address))


# --- merge (§7) -------------------------------------------------------------


def merge(session: Session, from_address: str, into_address: str) -> dict:
    """Merge `from` into `into` — mark `from` an ALIAS TOMBSTONE resolving to
    `into` forever (§7). Mechanics, all-or-nothing (no partial write on any
    rejection):

    - `into` is resolved to its TERMINAL target first and that target is stored,
      so alias chains stay depth-1; a merge whose terminal target equals `from`
      is rejected (cycle guard, same posture as `depends_on`).
    - `from` must not already be a tombstone (it is already merged away).
    - **State compatibility** (§7): legal iff `from.status == into.status` OR
      `from.status == planned`. Otherwise rejected — merging a *cancelled*
      milestone into a live one would resurrect dead spec versions through the
      alias, and an *achieved* one into a planned one would retroactively demote
      shipped stamps; those governance-history rewrites must be explicit re-stamps.
    - **Cross-anchor merges are allowed**; the tombstone stays at its original
      anchor, its name reserved there forever (the existing `create` check).
    - `from`'s dependency edges, BOTH directions, are re-pointed to `into`,
      deduplicated (self-edges from the re-point dropped), and the global cycle
      guard re-runs — the merge FAILS if the union would cycle.
    - Nothing is logged to the transition log (merge is not a lifecycle
      transition); `from`'s status is left as-is (the row is now an alias, and
      resolution never surfaces its status).

    Returns `{tombstone, target, reminder}` — the reminder restates that the
    platform never bulk-retags plugin data, so the caller must review affected
    rows agent-side (§7)."""
    frm = get(session, from_address)  # direct — the row to tombstone, as itself
    if frm.merged_into_id is not None:
        raise MilestoneMergeError(
            f"{address_of(frm)!r} is already a merge tombstone aliased to "
            f"{address_of(frm.merged_into)!r} (§7)"
        )
    into = resolve(session, into_address)  # follow to the TERMINAL target
    if into.id == frm.id:
        raise MilestoneMergeError(
            f"cannot merge {address_of(frm)!r} into itself — the terminal target "
            f"of {into_address!r} is {address_of(frm)!r} (cycle guard, §7)"
        )

    # State compatibility (§7) — the governance-history guard.
    if not (frm.status == into.status or frm.status == "planned"):
        raise MilestoneMergeError(
            f"cannot merge {address_of(frm)!r} ({frm.status}) into "
            f"{address_of(into)!r} ({into.status}) — merge is legal only when the "
            "statuses match or `from` is still planned. Merging a "
            f"{frm.status} milestone into a {into.status} one would rewrite "
            "governance history through the alias (a cancelled→live merge "
            "resurrects dead spec versions; an achieved→planned merge "
            "retroactively demotes shipped stamps). Re-stamp explicitly if that "
            "is truly intended (§7)."
        )

    # Re-point edges + tombstone `from` (the mechanics shared with replication
    # apply — see `_perform_merge`); raises `MilestoneMergeError` if the edge
    # union would cycle, before anything is written.
    _perform_merge(session, frm, into)
    session.flush()
    emit_event(
        session, EVENT_MILESTONE_MERGED, _merged_payload(frm, into)
    )
    return {
        "tombstone": to_row(frm),
        "target": address_of(into),
        "reminder": (
            f"{address_of(frm)} is now an alias of {address_of(into)}. The "
            "platform never bulk-retags plugin data — consumers' stored addresses "
            "stay put and reads agree via alias-set matching. Review affected "
            f"rows agent-side: list_artifact_versions(milestone={address_of(frm)}) "
            f"and milestone_status({address_of(frm)}); the platform cannot count "
            "plugin rows (§7)."
        ),
    }


def _perform_merge(session: Session, frm: Milestone, into: Milestone) -> None:
    """The merge MECHANICS shared by the `merge` verb and replication apply (§7,
    §9): re-point `from`'s edges (both directions) onto `into`, drop self-edges,
    dedupe against existing edges, re-run the GLOBAL cycle guard on the prospective
    union (raise `MilestoneMergeError` if it would cycle — nothing written),
    re-point any inbound alias so depth-1 holds in both orderings, then set
    `from`'s tombstone pointer + stamp the §6 LWW clock (the clock a later
    re-merge LWW-compares against). Status is left as-is (the row is now an alias;
    resolution never surfaces its status).

    The caller owns the DIFFERING guards: the `merge` verb pre-checks state
    compatibility + already-tombstone and lets a cycle surface as
    `MilestoneMergeError`; replication apply skips the state-compat re-check (it
    was decided at the authoring instance — apply converges, §9), LWW-resolves an
    already-tombstoned `from`, and translates a cycle into a park (§8.1)."""
    existing = list(session.scalars(select(MilestoneDependency)))
    touching = [
        e for e in existing if frm.id in (e.dependent_id, e.dependency_id)
    ]
    untouched = {
        (e.dependent_id, e.dependency_id) for e in existing if e not in touching
    }
    repointed: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for e in touching:
        d = into.id if e.dependent_id == frm.id else e.dependent_id
        dep = into.id if e.dependency_id == frm.id else e.dependency_id
        if d != dep:  # a self-edge produced by the re-point is dropped
            repointed.add((d, dep))
    if _has_cycle(untouched | repointed):
        raise MilestoneMergeError(
            f"merging {address_of(frm)!r} into {address_of(into)!r} would cycle "
            "the dependency DAG once edges are re-pointed — rejected, nothing "
            "written (§7)"
        )
    for e in touching:
        session.delete(e)
    for d, dep in repointed:
        if (d, dep) not in untouched:  # dedupe against edges already present
            session.add(MilestoneDependency(dependent_id=d, dependency_id=dep))

    # Re-point any tombstone that already aliases `from` onto `into` too, so the
    # depth-1 invariant holds in BOTH merge orderings (the spec spells out only
    # the into-is-a-tombstone direction; keeping `from`'s inbound aliases depth-1
    # is the symmetric requirement for `_follow_alias`'s single hop to be correct
    # when `from` is itself merged onward — §3/§7 interpretation).
    for inbound in session.scalars(
        select(Milestone).where(Milestone.merged_into_id == frm.id)
    ):
        inbound.merged_into_id = into.id

    frm.merged_into_id = into.id  # the tombstone; status stays as-is
    frm.lww_authored_at = _now()
    frm.lww_source_id = _local_source_id()


# --- aliases (§5) -----------------------------------------------------------


def aliases(session: Session, address: str) -> dict:
    """The transitive closure of tombstones resolving to the TERMINAL target of
    `address` (§5). Resolve the input first, then collect every tombstone chain
    pointing at that target — a milestone-keyed consumer matches stored stamps
    against the target's full alias set, or "reads via either address agree" is
    mechanically impossible. Depth-1 chains make this a reverse lookup in practice,
    but it is written closure-correct."""
    target = resolve(session, address)
    collected: dict[uuid.UUID, Milestone] = {}
    frontier = [target.id]
    while frontier:
        tid = frontier.pop()
        for m in session.scalars(
            select(Milestone).where(Milestone.merged_into_id == tid)
        ):
            if m.id not in collected:
                collected[m.id] = m
                frontier.append(m.id)
    return {
        "target": address_of(target),
        "aliases": sorted(address_of(m) for m in collected.values()),
    }


# --- serialization (the HTTP/MCP JSON shape) --------------------------------


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def to_row(milestone: Milestone) -> dict:
    """A milestone as a flat JSON row (the read API's shape). Carries the
    canonical `address` (the identity every consumer stores) and `id` (a soft
    reference an out-of-process consumer can capture), mirroring `scopes.to_row`.
    """
    return {
        "id": str(milestone.id),
        "address": address_of(milestone),
        "anchor": milestone.anchor.slug,
        "name": milestone.name,
        "outcome": milestone.outcome,
        "status": milestone.status,
        "target_date": _iso(milestone.target_date),
        "activated_at": _iso(milestone.activated_at),
        "achieved_at": _iso(milestone.achieved_at),
        "cancelled_at": _iso(milestone.cancelled_at),
        "merged_into": (
            address_of(milestone.merged_into)
            if milestone.merged_into_id is not None
            else None
        ),
        "created_at": _iso(milestone.created_at),
        "updated_at": _iso(milestone.updated_at),
    }


# --- replication: emit payloads (milestones.md §9) --------------------------
#
# Cross-instance identity is the CANONICAL ADDRESS (anchor slug + name), never
# the instance-local UUID (§9): every payload carries `anchor` (the slug apply
# re-resolves `anchor_scope_id` from) + `name`, and the `id` is deliberately
# OMITTED so nothing on the apply side can key on it. `authored_at` is the §6 LWW
# clock; the authoring `source_id` rides the ENVELOPE (`source`), so it is not
# duplicated here.


def to_replication_payload(milestone: Milestone) -> dict:
    """The `milestone.created` / `milestone.updated` event body — FULL ROW STATE
    keyed by the canonical address (§9). Carries everything apply needs to
    reconstruct the row: the anchor slug (re-resolved to a local `anchor_scope_id`
    at apply), the name, the mutable fields, and the `authored_at` LWW stamp."""
    return {
        "address": address_of(milestone),
        "anchor": milestone.anchor.slug,
        "name": milestone.name,
        "outcome": milestone.outcome,
        "status": milestone.status,
        "target_date": _iso(milestone.target_date),
        "activated_at": _iso(milestone.activated_at),
        "achieved_at": _iso(milestone.achieved_at),
        "cancelled_at": _iso(milestone.cancelled_at),
        "merged_into": (
            address_of(milestone.merged_into)
            if milestone.merged_into_id is not None
            else None
        ),
        "authored_at": _iso(milestone.lww_authored_at),
    }


def _transition_payload(
    milestone: Milestone,
    from_status: str,
    to_status: str,
    reason: str | None,
    authored_at: datetime,
) -> dict:
    """The `milestone.transitioned` body: FULL ROW STATE (so apply converges the
    row by LWW) PLUS the transition triple (from/to status, reason) and the
    transition's `authored_at` — the same instant stamped on the row clock and the
    log row, so a peer's LWW comparison + illegal-history reconstruction see the
    identical clock (§9)."""
    return {
        **to_replication_payload(milestone),
        "from_status": from_status,
        "to_status": to_status,
        "reason": reason,
        "authored_at": _iso(authored_at),
    }


def _dependency_payload(
    op: str, dependent: Milestone, dependency: Milestone
) -> dict:
    """The `milestone.dependency_changed` body — an add/remove edge DELTA (§9,
    documented choice): `op` ∈ {add, remove} + the two canonical addresses. A
    delta, NOT the dependent's full edge set, because a full-set replace would let
    a concurrent edge on the SAME dependent (added at the peer during a partition)
    be silently CLOBBERED on apply; a single-edge delta is idempotent (re-adding an
    existing edge / removing an absent one is a no-op) and never drops a
    concurrent edge, so both instances' independent edges survive convergence. The
    only conflict a delta cannot self-heal — an add that CYCLES the union — is
    exactly the §8.1 DAG race, which apply parks."""
    return {
        "op": op,
        "dependent": address_of(dependent),
        "dependency": address_of(dependency),
        "authored_at": _iso(_now()),
    }


def _merged_payload(frm: Milestone, into: Milestone) -> dict:
    """The `milestone.merged` body: the tombstone's canonical address + the
    TERMINAL target address (§9). Apply re-derives the alias + edge re-point from
    these two addresses (it never trusts a foreign UUID); `authored_at` is the LWW
    clock a later re-merge of the same `from` resolves against."""
    return {
        "from": address_of(frm),
        "into": address_of(into),
        "authored_at": _iso(frm.lww_authored_at),
    }


# --- replication: apply (address-keyed, §9) ---------------------------------
#
# The domain APPLY seam driven through the SDK's `ingest_delivery`
# (`replication.apply_platform_event` dispatches scope events here). Runs under
# origin suppression, so the direct row writes below never re-emit. Apply writes
# the row STATE DIRECTLY rather than replaying the lifecycle verbs (as scope apply
# reuses create/update): the verbs are legality-guarded and cannot express "set
# status=achieved with these timestamps" — a full-row LWW converge must (§6).
#
# Ordering posture, matched to scope apply's: a not-yet-replicated ANCHOR SCOPE
# (or a not-yet-replicated referenced milestone) surfaces as an ORDINARY retryable
# error — NOT `ParkNow` — so it self-heals the moment the referent's own event
# applies, exactly as scope apply leaves an unknown parent slug retryable. Only a
# permanent conflict that can never self-heal (a DAG/alias cycle in the union)
# raises `ParkNow` (§8.1). Apply NEVER parks on a mere LWW loss (§9).


class MilestoneAnchorPendingError(LookupError):
    """The anchor scope named in a replicated milestone event has not replicated
    to this instance yet (§9 ordering). A RETRYABLE error — never `ParkNow`: it
    self-heals when the anchor's own `scope.created` applies, matching scope
    apply's unknown-parent posture (the bounded retry exists for exactly this)."""


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _lww_incoming(payload: dict, envelope: dict) -> tuple[datetime, str]:
    """The incoming event's LWW key `(authored_at, source_id)` — `source_id` from
    the ENVELOPE (`source`), breaking an authored-at tie (§6)."""
    return (
        _parse_dt(payload["authored_at"]) or datetime.min,
        envelope.get("source") or "",
    )


def _lww_local(m: Milestone) -> tuple[datetime, str]:
    """The local row's LWW key. A NULL clock (a pre-replication row) sorts lowest,
    so any authored incoming event wins over it."""
    return (m.lww_authored_at or datetime.min, m.lww_source_id or "")


def _write_row_state(m: Milestone, payload: dict, envelope: dict) -> None:
    """Overwrite the row's MUTABLE state from a full-row payload + advance the LWW
    clock (§6). Never touches identity (`anchor_scope_id`/`name`) or the tombstone
    pointer (`merged_into_id` is owned by `milestone.merged` apply)."""
    m.outcome = payload["outcome"]
    m.status = payload["status"]
    m.target_date = _parse_date(payload["target_date"])
    m.activated_at = _parse_dt(payload["activated_at"])
    m.achieved_at = _parse_dt(payload["achieved_at"])
    m.cancelled_at = _parse_dt(payload["cancelled_at"])
    m.lww_authored_at = _parse_dt(payload["authored_at"])
    m.lww_source_id = envelope.get("source")


def _upsert_row(session: Session, envelope: dict) -> Milestone:
    """Address-keyed upsert of a full-row event (§9): re-resolve the anchor scope
    by SLUG, then insert (new) or LWW-converge (existing). A missing anchor scope
    raises the retryable `MilestoneAnchorPendingError`. On an LWW loss the local
    row is kept untouched — and NEVER parked (§9)."""
    payload = envelope["payload"]
    anchor = scopes.resolve(session, payload["anchor"])
    if anchor is None:
        raise MilestoneAnchorPendingError(
            f"anchor scope {payload['anchor']!r} for milestone "
            f"{payload['address']!r} has not replicated yet — retryable (§9)"
        )
    m = _by_anchor_name(session, anchor.id, payload["name"])
    if m is None:
        m = Milestone(anchor_scope_id=anchor.id, name=payload["name"])
        _write_row_state(m, payload, envelope)
        session.add(m)
        session.flush()
        return m
    if _lww_incoming(payload, envelope) > _lww_local(m):
        _write_row_state(m, payload, envelope)
        session.flush()
    return m


def _append_transition_idempotent(
    session: Session,
    m: Milestone,
    from_status: str,
    to_status: str,
    reason: str | None,
    authored_at: datetime | None,
    source_id: str | None,
) -> None:
    """Append the peer's transition to the log — the LWW LOSER's transition lands
    here too, never dropped (§9). Idempotent on redelivery: the
    `(milestone, from, to, authored_at, source_id)` tuple keys the dedupe."""
    exists = session.scalar(
        select(MilestoneTransition).where(
            MilestoneTransition.milestone_id == m.id,
            MilestoneTransition.from_status == from_status,
            MilestoneTransition.to_status == to_status,
            MilestoneTransition.authored_at == authored_at,
            MilestoneTransition.source_id == source_id,
        )
    )
    if exists is None:
        session.add(
            MilestoneTransition(
                milestone_id=m.id,
                from_status=from_status,
                to_status=to_status,
                reason=reason,
                authored_at=authored_at,
                source_id=source_id,
            )
        )
        session.flush()


def _check_illegal_history(session: Session, m: Milestone) -> None:
    """Reconstruct the CONVERGED transition path — every instance's transitions,
    ordered by `(authored_at, source_id)` — and flag any adjacent effective move
    ILLEGAL under §4 as first-class unreconciled state (§9). In single-instance
    operation each transition's `from` equals the prior `to`, so every adjacent
    `(prev.to, cur.to)` is a legal verb move and nothing flags; only concurrent
    partition-authored transitions can produce an adjacent illegal move (e.g. an
    earlier `cancel` an LWW-winning later `activate` reverses → cancelled→active).
    Apply CONVERGES the row regardless — this flags, it never parks (§9)."""
    rows = list(
        session.scalars(
            select(MilestoneTransition)
            .where(MilestoneTransition.milestone_id == m.id)
            .order_by(
                MilestoneTransition.authored_at,
                MilestoneTransition.source_id,
            )
        )
    )
    for prev, cur in zip(rows, rows[1:]):
        move = (prev.to_status, cur.to_status)
        if move[0] == move[1] or move in LEGAL_TRANSITIONS:
            continue
        _flag_unreconciled(session, m, prev, cur)


def _flag_unreconciled(
    session: Session,
    m: Milestone,
    prev: MilestoneTransition,
    cur: MilestoneTransition,
) -> None:
    """Record (once per distinct illegal move on a milestone) a first-class
    unreconciled row for agent triage (§9) — deduped on the illegal (from,to) pair
    so redelivery never piles duplicates."""
    move = [prev.to_status, cur.to_status]
    for u in session.scalars(
        select(MilestoneUnreconciled).where(
            MilestoneUnreconciled.milestone_id == m.id
        )
    ):
        if u.detail and u.detail.get("illegal_move") == move:
            return
    session.add(
        MilestoneUnreconciled(
            milestone_id=m.id,
            reason=(
                f"converged transition history implies {move[0]}->{move[1]}, "
                "illegal under the §4 legality table — concurrent partition "
                "transitions need agent triage (§9)"
            ),
            detail={
                "illegal_move": move,
                "earlier": {
                    "from_status": prev.from_status,
                    "to_status": prev.to_status,
                    "authored_at": _iso(prev.authored_at),
                    "source_id": prev.source_id,
                },
                "later": {
                    "from_status": cur.from_status,
                    "to_status": cur.to_status,
                    "authored_at": _iso(cur.authored_at),
                    "source_id": cur.source_id,
                },
            },
        )
    )
    session.flush()


def _apply_transitioned(session: Session, envelope: dict) -> None:
    """Apply `milestone.transitioned` (§9): LWW-converge the row from full state,
    ALWAYS append the transition to the log (loser included), then run the §4
    illegal-history check. Converges — never parks on LWW loss."""
    payload = envelope["payload"]
    m = _upsert_row(session, envelope)
    _append_transition_idempotent(
        session,
        m,
        payload["from_status"],
        payload["to_status"],
        payload.get("reason"),
        _parse_dt(payload["authored_at"]),
        envelope.get("source"),
    )
    _check_illegal_history(session, m)


def _apply_dependency_changed(session: Session, envelope: dict) -> None:
    """Apply `milestone.dependency_changed` (§9) — the add/remove edge delta.
    Both endpoints resolve through any alias (a not-yet-replicated milestone
    surfaces as a retryable resolution error, self-healing). An `add` whose union
    with the local edge set CYCLES is the §8.1 DAG race: `ParkNow` (reject +
    park), keeping the dependency walk loop-free by construction."""
    payload = envelope["payload"]
    dependent = resolve(session, payload["dependent"])
    dependency = resolve(session, payload["dependency"])
    op = payload["op"]
    if op == "add":
        if dependent.id == dependency.id:
            return  # a self-edge after alias resolution — nothing to add
        if _edge(session, dependent.id, dependency.id) is None:
            edges = _all_edges(session)
            edges.add((dependent.id, dependency.id))
            if _has_cycle(edges):
                raise ParkNow(
                    f"incoming dependency {payload['dependent']!r} -> "
                    f"{payload['dependency']!r} cycles the local dependency DAG "
                    "(each side passed its own guard; the union cycles) — "
                    "rejected and parked for triage (§8.1/§9)"
                )
            session.add(
                MilestoneDependency(
                    dependent_id=dependent.id, dependency_id=dependency.id
                )
            )
            session.flush()
    elif op == "remove":
        edge = _edge(session, dependent.id, dependency.id)
        if edge is not None:
            session.delete(edge)
            session.flush()
    else:
        raise ValueError(
            f"milestone.dependency_changed: unknown op {op!r} (add|remove)"
        )


def _apply_merged(session: Session, envelope: dict) -> None:
    """Apply `milestone.merged` (§9). `from` is resolved by DIRECT address (the
    tombstone itself), `into` to its TERMINAL target — a not-yet-replicated either
    side is retryable. Handles the spec's named apply cases:

      * `from` already a tombstone pointing ELSEWHERE — LWW on `merged_into` by
        `authored_at` (`source_id` tiebreak); the loser is a clean no-op.
      * an application that would CYCLE the alias graph (`into`'s terminal is
        `from`) or the dependency DAG (the edge union) — `ParkNow` (reject + park,
        §8.1), keeping alias traversal + the dependency walk loop-free.
      * STATE COMPATIBILITY is NOT re-checked: it was decided at the authoring
        instance (the `merge` verb's guard), and apply CONVERGES rather than
        re-litigating an authored decision (§9)."""
    payload = envelope["payload"]
    frm = get(session, payload["from"])  # direct — the tombstone row itself
    into = resolve(session, payload["into"])  # follow to the terminal target
    incoming = (
        _parse_dt(payload["authored_at"]) or datetime.min,
        envelope.get("source") or "",
    )
    if frm.merged_into_id is not None:
        if frm.merged_into_id == into.id:
            return  # idempotent replay — already aliased to this target
        if incoming <= _lww_local(frm):
            return  # a competing local merge wins by LWW — keep it (§9)
    if into.id == frm.id:
        raise ParkNow(
            f"merging {payload['from']!r} into {payload['into']!r} cycles the "
            "alias graph (the terminal target resolves back to `from`) — "
            "rejected and parked (§8.1/§9)"
        )
    try:
        _perform_merge(session, frm, into)
    except MilestoneMergeError as exc:
        raise ParkNow(
            f"merge {payload['from']!r} -> {payload['into']!r} would cycle the "
            f"dependency DAG once edges re-point ({exc}) — rejected and parked "
            "(§8.1/§9)"
        ) from exc
    # Stamp the AUTHORED clock (not `_perform_merge`'s apply-time `_now()`), so a
    # later re-merge of this `from` LWW-compares against the true authored instant.
    frm.lww_authored_at = incoming[0]
    frm.lww_source_id = envelope.get("source")
    session.flush()


def apply_milestone_event(session: Session, envelope: dict) -> None:
    """The milestone replication APPLY function (§9) — the domain seam
    `replication.apply_platform_event` dispatches milestone events to, driven
    through the SDK's `ingest_delivery` under origin suppression. Address-keyed,
    LWW-converging (§6), parking only a permanent cycle conflict (§8.1) — never a
    mere LWW loss, and never inventing a legal history (§9)."""
    event_type = envelope["event_type"]
    if event_type in (EVENT_MILESTONE_CREATED, EVENT_MILESTONE_UPDATED):
        _upsert_row(session, envelope)
    elif event_type == EVENT_MILESTONE_TRANSITIONED:
        _apply_transitioned(session, envelope)
    elif event_type == EVENT_MILESTONE_DEPENDENCY_CHANGED:
        _apply_dependency_changed(session, envelope)
    elif event_type == EVENT_MILESTONE_MERGED:
        _apply_merged(session, envelope)
    else:
        raise ValueError(
            f"milestone replication apply: unknown event_type {event_type!r}"
        )


# --- unreconciled state read (§9) -------------------------------------------


def list_unreconciled(session: Session) -> list[dict]:
    """Every first-class unreconciled milestone row, oldest first (§9) — the
    agent-triage read (the milestone analogue of governance's unreconciled
    decisions, and of the replication parked-events read). An empty list is the
    standing invariant to watch."""
    rows = session.scalars(
        select(MilestoneUnreconciled).order_by(
            MilestoneUnreconciled.created_at, MilestoneUnreconciled.id
        )
    )
    return [
        {
            "milestone": address_of(u.milestone),
            "reason": u.reason,
            "detail": u.detail,
            "created_at": _iso(u.created_at),
        }
        for u in rows
    ]
