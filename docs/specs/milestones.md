# Milestones

> **Status: draft.** The platform-owned milestone registry — identity, lifecycle,
> resolution, merge — plus the contracts governance and PM consume from it. Read
> `architecture.md` and `scope-namespace.md` first; the governance consumer
> contract here amends `governance-plugin.md` §4/§5.

## 1. Purpose

A **milestone** is a verifiable, integrated delivery checkpoint — *Spanish
beta*, *v1 launch*, *RTL-ready*. Today it exists only as a free-text tag on PM
work items and a **soft** release slug stamped on governance artifact versions
(#141/#143). Three pressures push it to a first-class platform primitive:

1. **Identity drift.** Two sessions minted the same real milestone twice —
   `v1-launch` (37 items) and `turtlesedge/turtletracks/v1-launch` (104 items) —
   because the owner spoke shorthand and the tag surface stored it literally.
   Every roll-up read of that milestone is now silently split.
2. **Spec validity across releases.** A spec revision targeted at a *future*
   milestone must be draftable while the previous version stays the canonical
   spec for the *active* milestone. The version DAG can hold both; nothing today
   can say which leaf is valid *when*.
3. **Multi-release delivery planning** (the TurtleTracks internationalization
   pressure test): milestones need lifecycle, dependencies, cross-scope
   aggregation, and explicit — never automatic — achievement.

The **platform owns the registry** for the same reason it owns scopes: it is a
shared identity primitive several plugins must resolve identically (PM tags,
governance version stamps, marketing follow-through). Plugins reference
milestones by address and read the registry; they never own it.

**Boundaries (the fence, held deliberately):** *initiative* = durable outcome
stream (PM); *phase* = ordered workstream within an initiative (PM);
*milestone* = verifiable checkpoint, potentially spanning phases, initiatives,
scopes, and non-code work; *release* = a deployed artifact — a milestone may
target one but must never require one.

## 2. The Milestone model

- `anchor_scope_id` — FK to the platform `Scope`; the anchor's slug must be
  **1 or 2 segments** (org- or repo-level; the check is by segment count, which
  the address grammar below depends on — expected kinds `org` | `project`).
  No portfolio/global level. The anchor is the **namespace and resolution
  home, not a membership fence** — work from any scope may contribute (open
  membership; membership itself lives in PM, §6.2).
- `name` — **slash-free** slug; canonical lowercase, case-insensitive input
  (the #134/#139 convention, composing with scope-slug folding in addresses);
  unique within its anchor scope, **including merge tombstones** (§7).
- **Address grammar — self-describing by segment count** (possible only because
  names are slash-free): `v1-launch` = bare name, resolved via context (§3);
  `turtlesedge/v1-launch` = org-anchored; `turtlesedge/turtletracks/v1-launch`
  = repo-anchored. No parse ambiguity anywhere.
- `outcome` — the outcome statement / exit-criteria prose. Machine-checkable
  and human criteria are **not** modeled here — they are PM work items (§6.2).
- `status` — `planned` | `active` | `achieved` | `cancelled`, with
  `activated_at` / `achieved_at` / `cancelled_at` and optional `target_date`.
  **Transitions are explicit verbs; nothing is automatic** — all tagged items
  going terminal does not achieve a milestone, and achieving one cancels no
  work. **Concurrent active milestones on one anchor are legal and intended**
  (the i18n scenario runs several release lines at once).
- **Transition log** — an append-only record per transition: from/to status,
  authored-at timestamp (the replication LWW clock, §9), and optional
  free-text `reason`. This is where the lifecycle verbs' `reason` lands.
- `depends_on` — milestone→milestone edges (a cycle-guarded DAG): *spanish-beta
  depends on localization-foundation*. **Cross-anchor edges are allowed** (the
  anchor is not a fence); the cycle guard runs over the global edge set. Gates
  are for **readiness reads only** — they never block lifecycle verbs (§4).
- `merged_into_id` — self-FK alias tombstone (§7).
- `created_at` / `updated_at`.

## 3. Resolution (the drift killer)

Shorthand is a **legitimate input format** — humans speak it in conversation
and agents relay it — so the tools resolve it; **storage is always canonical**.

- `resolve(ref, context_scope=None)`: a 2- or 3-segment address resolves
  directly. A **bare name** requires a context and walks it most-specific-first:
  the context scope is first normalized to its nearest ancestor-or-self
  **repo-level** scope (an initiative context walks up to its repo; an org
  context skips the repo step), then the walk tries that repo anchor, then its
  org anchor; first hit wins (repo shadows org, predictably).
- **Bare names never resolve outside the walk.** A walk miss hard-fails; other
  anchors' same-named milestones appear as *suggestions only*, never as an
  automatic resolution. A bare name with **no context always hard-fails**,
  listing candidates — even a unique one. (Uniform strictness: providing
  context must never make resolution stricter than omitting it.)
- **Unknown never mints.** An unresolvable ref **hard-fails** listing near-miss
  candidates (`unknown milestone 'v1-lanch' — did you mean
  turtlesedge/turtletracks/v1-launch (active)?`). There is no auto-vivify at
  any surface (mirrors the scope `resolve` posture).
- A ref resolving to a merge tombstone follows the alias to its **terminal
  target** (tombstones store the terminal target directly, so chains stay
  depth-1 — §7) and returns it, noting the traversed alias. Stamping or
  tagging *via* a tombstoned address therefore stores the target's canonical
  address — a merged-away name can never accrue new references.

## 4. Operations (the milestone service)

- `resolve(ref, context=None)` — as §3. `get(address)` — the row; on a
  tombstone address, returns the tombstone itself with its target noted (the
  audit read; `resolve` is the one that follows the alias).
- `list(anchor=None, status=None)` — subtree-filters by anchor; **excludes
  tombstones by default** (`include_merged=` opts in).
- `create(anchor, name, outcome, target_date=None)` — the only mint path;
  enforces slash-free name, 1-or-2-segment anchor, and uniqueness **against
  live rows and tombstones alike** (a tombstoned name is reserved forever; the
  error names the alias target).
- `activate` / `achieve` / `cancel` — explicit lifecycle verbs (optional
  `reason`, recorded on the transition log). Legal moves:
  planned→active→achieved; planned|active→cancelled. `achieve` on a *planned*
  milestone is **rejected** ("activate first" — never auto-activates). No
  transition is ever implied by member-item state. **Dependencies never gate
  transitions**: activating or achieving with unachieved (or cancelled)
  dependencies succeeds, but the response carries a warning listing them.
  Cancelling an **active** milestone is a deliberate retraction — the response
  warns that governance versions stamped with it will demote (§6.1.6).
- `add_dependency` / `remove_dependency` — cycle-guarded (globally, §2) DAG
  edges. A dependency on a **cancelled** milestone is *not* silently ignored:
  readiness reads surface it as `blocked_by_cancelled` until explicitly
  removed — silent ignoring would hide real plan breakage.
- `merge(from_address, into_address)` — §7.
- `update` — outcome / target_date / display fields; never identity.

## 5. Surfaces

- **HTTP read/resolve API** behind the trust gate (for out-of-process plugins,
  exactly like the scope surface):
  - `GET /milestones`, `GET /milestones/{address}`,
    `GET /milestones/{address}/dependencies`.
  - `GET /milestones/resolve?ref=&context=` — single-ref resolution.
  - `POST /milestones/resolve-batch` — body of refs → `{ref: {address, status,
    resolved_via_alias}}` in one round-trip. This is the read governance's
    canonicality computation uses (§6.1) — per-stamp fan-out would be N
    round-trips per artifact read.
  - `GET /milestones/{address}/aliases` — the transitive closure of tombstones
    resolving to this milestone. Milestone-keyed consumer reads match stored
    stamps/tags against **the target's full alias set**, or §7's "reads via
    either address agree" is mechanically impossible.
- **MCP tools on the platform `main` surface**: `create_milestone`,
  `resolve_milestone`, `list_milestones` (registry rows — distinct from PM's
  work roll-up read of the same name; prefixes disambiguate),
  `activate_milestone`, `achieve_milestone`, `cancel_milestone`,
  `merge_milestone`, dependency verbs. *(First cut may land the service + read
  API + create/resolve/lifecycle; merge and dependencies can follow.)*
- **Events — deferred.** The platform has **no event bus**: the only spec'd
  signed webhook bus is governance's own (governance-plugin.md §7, a plugin
  store), and the platform's replication-class scope stream is pairing-scoped
  peer replication, not a subscribable channel. Milestone lifecycle events
  (`milestone.*`) therefore wait on a **platform-owned webhook-class bus** —
  its own component spec, in the shape of governance §7 (HMAC, contract
  version, outbox), at which point `milestone.*` types join the drift-guarded
  `EVENT_TYPES` registries in both packages per replication-continuity §3.2,
  and webhook-class emission stays separate from the replication-class stream
  (origin suppression makes replication events unusable as notifications).
  **Until then, consumers poll the read API** — PM's lifecycle-event design
  (pm#64) should treat the registry transition log as the source it will
  eventually subscribe to.

## 6. Consumer contracts

### 6.1 Governance — spec validity across milestones

Amends `governance-plugin.md` §4/§5. The `ArtifactVersion.milestone` stamp
graduates from soft annotation to **resolution key**. The load-bearing change:
**canonicality stops being bare DAG-leaf-ness and becomes a function of
milestone state** — so every write default that assumed leaf = canonical is
restated here.

1. **Validated at mint.** `register_artifact` / `revise_artifact` resolve the
   milestone ref against the platform and store the canonical address; unknown
   refs hard-fail. Bare-name context = the artifact's governing scope,
   normalized per §3 — an artifact with **list or `*` governs has no bare-name
   context**: bare refs are rejected at mint (full address required).
   Stamping a version with an **achieved or cancelled** milestone is rejected
   absent an explicit override flag — a post-hoc stamp rewrites what
   `list_artifact_versions(milestone=…)` reports a release shipped as.
   This supersedes the "stamped verbatim, never resolved" posture of #141.
2. **State buckets.** Versions bucket by their stamp's milestone status, read
   from the platform (batch endpoint, §5):
   - **eligible** — stamp absent, or milestone `active` / `achieved`;
   - **pending** — milestone `planned`;
   - **dead** — milestone `cancelled`;
   - **legacy** — stamp doesn't resolve (pre-registry verbatim stamps, §7):
     treated as *absent* for canonicality (annotation-only), flagged on
     version reads for agent-driven backfill.
   A milestone-status read failure is a **hard error on the governance read**
   — never treat an unreadable stamp as absent; a transient platform outage
   must not flip canonicality (co-located loopback reads, same rationale as
   replication-continuity §6.1).
3. **Canonical = the leaf of the eligible subgraph.** Take the version DAG
   induced on *eligible* versions only: the canonical version is its leaf.
   Pending/dead versions do not supersede for canonicality purposes —
   drafting the v2 spec no longer dethrones the v1 spec. If the eligible
   subgraph has **multiple leaves** (e.g. an active-stamped fix to v1 and a
   just-activated v2 both fork from v1), that is a **genuine competition**:
   the default read returns the newest leaf by version creation **plus an
   explicit `competing_leaves` warning** — never a silent tie-break — and
   `resolve_artifact` collapses it (agent judgment merges the lines). Note
   this competition can *appear at activation time with no governance write*,
   which is exactly why it must be surfaced on read rather than checked on
   write. Two parallel pending forks of one parent are legal at mint (v2 and
   v3 drafts) — they surface as competition when their milestones activate,
   because neither contains the other's changes.
4. **Write defaults follow canonicality, not leaf-ness.** `revise_artifact`'s
   `supersedes` **defaults to the current canonical version** — *not* the DAG
   leaf, which may be a pending draft. (Old default: a typo fix to the active
   spec would land as an unstamped child of the pending leaf and instantly
   leak unreleased content into canonical.) Superseding a pending version is
   legal but must be **explicit**, and a revision whose supersedes-target is
   pending or dead **must carry an explicit stamp** (inherit-or-state; an
   unstamped child of a non-eligible version is rejected) — otherwise the
   child would be eligible-absent and instantly canonical.
5. **Per-milestone reads.** `get_artifact(..., milestone=REF)` returns the
   version valid *for* that milestone: the leaf of the subgraph induced on
   versions stamped with it (alias-set matching, §5) — multiple stampings
   along one line are the normal iterate-toward-a-release shape, and the leaf
   wins; competing stamped leaves are surfaced as in item 3. No stamped
   version → the canonical version. The default read is unchanged (canonical).
6. **Promotion and demotion are implicit.** planned→active moves a pending
   version to eligible **by resolution** — no governance write, no
   cross-plugin choreography; the platform transition *is* the promotion.
   Symmetrically, the bucket formula is **authoritative for cancellation**:
   cancelling a *planned* milestone strands only never-canonical drafts (dead
   branches — auditable; recover by an explicitly-stamped revision
   superseding the dead leaf, per item 4), but cancelling an **active**
   milestone *demotes* its stamped versions and canonicality reverts to the
   prior eligible leaf. That demotion is deliberate retraction semantics; the
   registry's `cancel` verb warns (§4), and version reads flag the dead line.
   `resolve_artifact` keeps its role for genuine competing leaves *within*
   the eligible bucket.

### 6.2 PM — validated tagging (private plugin; adoption tracked in snowline-pm)

- `set_milestone` / item-create **validate + resolve** via the platform, using
  the item's primary scope as bare-name context (normalized per §3; org-level
  items walk org-only); store canonical. The original drift becomes
  inexpressible.
- `milestone_status` / `whats_next(milestone=)` resolve refs identically,
  match stored tags against the resolved target's **alias set** (§5), and
  surface the canonical matched address (closes the misread reported in
  feedback `489a30ff`). Items tagged before a merge keep their stored address;
  alias-set matching keeps every read whole with no retag.
- An item keeps **one** target milestone; a `required` | `optional` membership
  attribute (default required) is PM-side. Later milestones depend on earlier
  ones rather than re-counting foundation items.
- **Manual exit criteria are human-owned work items** (`set_human_owned`)
  tagged required to the milestone — no parallel checklist system;
  `milestone_status` stays one computation.
- Readiness reporting (required-remaining, blockers incl. `blocked_by_cancelled`
  dependencies, scope churn — never a lone percentage) and roll-forward audit
  history (original milestone, new milestone, reason, actor) are PM-side over
  the registry.
- pm#68's release verb folds in: "release" = `achieve` on the registry plus
  PM's lifecycle event (pm#64; polling until the platform bus exists, §5).
  #68's "slug owned by nobody" posture is **superseded** by this registry —
  the drift evidence is what changed the call.

## 7. Merge + migration (pre-registry drift)

- `merge(from, into)` marks `from` as an **alias tombstone**: resolving it
  returns `into`, forever. Mechanics:
  - `into` must not itself be a tombstone — it is resolved to its **terminal
    target** first and that target is stored, so alias chains stay depth-1;
    a merge whose terminal target equals `from` is rejected (cycle guard,
    same posture as `depends_on`).
  - **Cross-anchor merges are allowed** (org-anchored into repo-anchored and
    vice versa — the known drift case *is* cross-anchor); the tombstone stays
    at its original anchor, occupying its name there forever (§4 `create`).
  - **State compatibility is required**: `merge` is legal iff
    `from.status == into.status` **or** `from.status == planned`. This is
    what keeps merge from silently rewriting governance history — merging a
    *cancelled* milestone into a live one would resurrect dead spec versions
    through the alias, and merging an *achieved* one into a planned one would
    retroactively demote shipped stamps to pending. Those cases are rejected;
    re-stamp explicitly if that's truly intended.
  - `from`'s `depends_on` edges (both directions) are re-pointed to `into`,
    deduplicated, and the cycle guard re-runs — the merge **fails** if the
    union would cycle.
  - The platform **never bulk-retags plugin data** (it cannot write plugin
    stores — the LLM is the integration runtime); consumers' stored addresses
    stay put and reads agree via alias-set matching (§5). The merge response
    reminds the caller to review affected rows agent-side
    (`list_artifact_versions(milestone=from)`, `milestone_status(from)`) —
    the platform cannot count them itself.
- Known first customer: the `v1-launch` split above. Its reconciliation is
  **owner-manual by explicit decision** — the alias makes both addresses read
  as one milestone without touching a single item.
- Bootstrap: the registry is seeded by **explicit creates** from observed tags,
  not auto-imported — same no-vivify posture as everywhere else. Pre-registry
  stamps/tags that never get a registry row remain **legacy** (annotation-only,
  §6.1.2) until backfilled.

## 8. Out of scope

- Membership storage, required/optional, readiness computation, roll-forward
  history — PM's, over the registry.
- Artifact content/version mechanics beyond §6.1's resolution change.
- Release/deploy tracking — a milestone may name a target release in prose;
  releases are not modeled.
- **The platform event bus** — a separate component spec (§5 Events); until it
  exists there are no `milestone.*` emissions of any class except replication.
- Auto-achievement, progress percentages, and any scheduling authority:
  milestones must not become a second priority system — an explicit
  milestone *focus* filter on `whats_next` is PM-side and visibly stateful.

## 9. Implementation note

A new platform-owned table + service beside scopes (Postgres + Alembic). The
governance change is a resolution-layer change plus stamp validation — the
schema already carries `milestone` (#143). The HTTP surface mirrors the scope
read/resolve API. PM adoption is its own work items on
`snowlinedev/snowline-pm` (this spec is the contract they build against).

**Replication** (per `replication-continuity.md`; milestones are *not* the
easy adopter scopes are — a lifecycle state machine plus a mutable DAG needs
the §6 rules stated, not implied):

- **Cross-instance identity is the canonical address** `(anchor slug, name)` —
  UUIDs are instance-local, apply is address-keyed, and `anchor_scope_id` is
  re-resolved from the anchor slug at apply (the scope-slug identity rule of
  replication §7.1 carries over).
- **Event vocabulary**: `milestone.created` / `milestone.updated` /
  `milestone.transitioned` / `milestone.dependency_changed` /
  `milestone.merged`, each carrying **full row state** plus an **authored-at
  stamp** — the LWW clock (replication §6: pure two-event resolution, LWW by
  authored-at, `source_id` tiebreak). `update` and dependency edits replicate
  too — a verb with no event silently never replicates.
- **Concurrent transitions** (e.g. `activate` on the hub, `cancel` on the
  spoke, during a partition): the row converges by LWW per replication §6,
  the loser's transition stays in the transition log, and a pair that is
  **illegal under §4's legality table** (the converged history implies e.g.
  cancelled→active) is flagged as **first-class unreconciled state** for
  agent triage — apply never silently invents a legal history, and never
  parks on mere LWW loss. Status disagreement between instances also means
  §6.1 canonicality disagrees until convergence — one more reason apply must
  converge rather than park.
- **DAG races**: `add_dependency(A→B)` and `add_dependency(B→A)` (or
  `merge(a,b)` / `merge(b,a)`) can each pass their local cycle guard and
  cycle only in the union — the second-applied edge is **rejected and
  parked** (replication §8.1 posture) as unreconciled state, keeping §3's
  alias traversal and the dependency walk loop-free by construction.

## 10. Acceptance criteria

Adapted from the internationalization pressure test:

- Create `localization-foundation`, `spanish-beta`, `spanish-ga`, `rtl-ready`
  anchored at `turtlesedge/turtletracks`; beta depends on foundation, GA on
  beta, RTL on foundation; a cycle is rejected.
- `resolve("spanish-beta", context=turtlesedge/turtletracks)` returns the
  canonical address; a typo'd ref hard-fails with a near-miss suggestion and
  **nothing is minted**; a bare name with no context hard-fails even with a
  unique candidate (suggested, not resolved); with both `org/x` and
  `org/repo/x` registered, a bare `x` in repo context resolves to the repo
  anchor; an initiative-scoped context resolves via its repo.
- A mixed-case ref (`TurtlesEdge/turtletracks/V1-Launch`) resolves to the
  canonical lowercase address; `create` differing only by case from an
  existing name (or tombstone) fails as a duplicate.
- Governance: revising a spec with a planned-milestone stamp leaves the old
  version canonical and the new one pending; a subsequent `revise_artifact`
  with no `supersedes` targets the **canonical** version, not the pending
  leaf; an unstamped revision explicitly superseding a pending version is
  rejected; `get_artifact(milestone=…)` returns the per-milestone version;
  activating the milestone flips canonical **with no governance write**;
  eligible-subgraph competition (two eligible leaves) is returned with a
  `competing_leaves` warning, never a silent pick; cancelling a *planned*
  milestone leaves canonical untouched; cancelling an **active** one demotes
  its stamped versions with the verb-level warning.
- `merge(a, b)`: reads via either address agree (alias-set matching);
  resolution of `a` returns `b` with the alias noted; `create` on the
  tombstoned name fails; a merge violating state compatibility
  (cancelled→active, achieved→planned) is rejected; no plugin rows were
  rewritten.
- `achieve` is explicit: all-items-terminal alone never achieves; `achieve` on
  a planned milestone is rejected; activating with an unachieved dependency
  succeeds with a warning; cancelling a milestone cancels no initiative and no
  work items (asserted PM-side at adoption).
- With the platform milestone read unavailable, governance's canonical read
  fails loudly — a version is never treated as unstamped because its stamp
  couldn't be resolved.
