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
)
from snowline_platform.scopes import canonical_slug, validate_slug

# --- name / status contract (§2) --------------------------------------------

# A milestone `name` is a SLASH-FREE slug — a single scope-style segment (the
# GitHub identifier set lowercased). Reusing the scope segment shape keeps the
# folding rules identical; the absence of `/` in the charset is exactly what
# makes the address grammar self-describing by segment count (§2).
_NAME_SEG = r"[._-]*[a-z0-9][a-z0-9._-]*"
NAME_RE = re.compile(rf"^{_NAME_SEG}$")

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
    return m


# --- lifecycle (§4) ---------------------------------------------------------


def _log_transition(
    session: Session,
    m: Milestone,
    from_status: str,
    to_status: str,
    reason: str | None,
) -> None:
    session.add(
        MilestoneTransition(
            milestone_id=m.id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
        )
    )


def activate(
    session: Session, address: str, reason: str | None = None
) -> Milestone:
    """planned→active (§4). Rejects any other source status; records the
    transition (with optional `reason`). Nothing is ever automatic."""
    m = get(session, address)
    if m.status != "planned":
        raise IllegalTransitionError(
            f"cannot activate {address!r} from status {m.status!r} — "
            "only a planned milestone activates (§4)"
        )
    frm = m.status
    m.status = "active"
    m.activated_at = _now()
    _log_transition(session, m, frm, m.status, reason)
    session.flush()
    return m


def achieve(
    session: Session, address: str, reason: str | None = None
) -> Milestone:
    """active→achieved (§4). `achieve` on a PLANNED milestone is REJECTED —
    "activate first"; it never auto-activates, and no member-item state ever
    implies achievement. Records the transition."""
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
    frm = m.status
    m.status = "achieved"
    m.achieved_at = _now()
    _log_transition(session, m, frm, m.status, reason)
    session.flush()
    return m


def cancel(
    session: Session, address: str, reason: str | None = None
) -> Milestone:
    """planned|active→cancelled (§4) — a deliberate retraction. Records the
    transition. (Cancelling an ACTIVE milestone demotes governance versions
    stamped with it, §6.1.6 — that warning surfaces once the governance consumer
    lands; deferred here.)"""
    m = get(session, address)
    if m.status not in ("planned", "active"):
        raise IllegalTransitionError(
            f"cannot cancel {address!r} from status {m.status!r} — "
            "only a planned or active milestone is cancelled (§4)"
        )
    frm = m.status
    m.status = "cancelled"
    m.cancelled_at = _now()
    _log_transition(session, m, frm, m.status, reason)
    session.flush()
    return m


def update(
    session: Session,
    address: str,
    *,
    outcome=_UNSET,
    target_date=_UNSET,
) -> Milestone:
    """Modify display fields — `outcome` / `target_date` — NEVER identity (§4).
    A provided value of `None` CLEARS the field; omitting the argument leaves it
    unchanged. Raises `MilestoneNotFoundError` if unknown."""
    m = get(session, address)
    if outcome is not _UNSET:
        m.outcome = outcome
    if target_date is not _UNSET:
        m.target_date = target_date
    session.flush()
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

    # Re-point `from`'s edges (both directions) onto `into`, drop self-edges,
    # dedupe, and re-run the GLOBAL cycle guard on the prospective union — fail
    # whole if it would cycle (§7). Nothing is written until the guard passes.
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
    session.flush()
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
