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

- `anchor_scope_id` — FK to the platform `Scope`; **org- or repo-level only**
  (no portfolio/global level). The anchor is the **namespace and resolution
  home, not a membership fence** — work from any scope may contribute (open
  membership; membership itself lives in PM, §6.2).
- `name` — **slash-free** slug; canonical lowercase, case-insensitive input
  (the #134/#139 convention); unique within its anchor scope.
- **Address grammar — self-describing by segment count** (possible only because
  names are slash-free): `v1-launch` = bare name, resolved via context (§3);
  `turtlesedge/v1-launch` = org-anchored; `turtlesedge/turtletracks/v1-launch`
  = repo-anchored. No parse ambiguity anywhere.
- `outcome` — the outcome statement / exit-criteria prose. Machine-checkable
  and human criteria are **not** modeled here — they are PM work items (§6.2).
- `status` — `planned` | `active` | `achieved` | `cancelled`, with
  `achieved_at` / `cancelled_at` and optional `target_date`. **Transitions are
  explicit verbs; nothing is automatic** — all tagged items going terminal does
  not achieve a milestone, and achieving one cancels no work.
- `depends_on` — milestone→milestone edges (a cycle-guarded DAG): *spanish-beta
  depends on localization-foundation*. Gates for readiness reads; achieving a
  prerequisite visibly unblocks dependents.
- `merged_into_id` — self-FK alias tombstone (§7).
- `created_at` / `updated_at`.

## 3. Resolution (the drift killer)

Shorthand is a **legitimate input format** — humans speak it in conversation
and agents relay it — so the tools resolve it; **storage is always canonical**.

- `resolve(ref, context_scope=None)`: a 2- or 3-segment address resolves
  directly. A **bare name** walks the context most-specific-first: the context's
  repo scope, then its org; first hit wins (repo shadows org, predictably).
- **Unknown never mints.** An unresolvable ref **hard-fails** listing near-miss
  candidates (`unknown milestone 'v1-lanch' — did you mean
  turtlesedge/turtletracks/v1-launch (active)?`). A bare name with no context
  and multiple cross-anchor candidates likewise fails listing them. There is no
  auto-vivify at any surface (mirrors the scope `resolve` posture).
- A ref resolving to a merged milestone returns the merge **target**, noting
  the traversed alias.

## 4. Operations (the milestone service)

- `resolve(ref, context=None)` / `get(address)` / `list(anchor=None,
  status=None)` — reads; `list` subtree-filters by anchor.
- `create(anchor, name, outcome, target_date=None)` — the only mint path;
  enforces slash-free name, org|repo anchor, uniqueness.
- `activate` / `achieve` / `cancel` — explicit lifecycle verbs (optional
  free-text `reason`, recorded). Legal moves: planned→active→achieved;
  planned|active→cancelled. No transition is ever implied by member-item state.
- `add_dependency` / `remove_dependency` — cycle-guarded DAG edges.
- `merge(from_address, into_address)` — §7.
- `update` — outcome / target_date / display fields; never identity.

## 5. Surfaces

- **HTTP read/resolve API** behind the trust gate (for out-of-process plugins,
  exactly like the scope surface): `GET /milestones`, `GET
  /milestones/{address}`, `GET /milestones/{address}/dependencies`, `GET
  /milestones/resolve?ref=&context=`.
- **MCP tools on the platform `main` surface**: `create_milestone`,
  `resolve_milestone`, `list_milestones` (registry rows — distinct from PM's
  work roll-up read of the same name; prefixes disambiguate),
  `activate_milestone`, `achieve_milestone`, `cancel_milestone`,
  `merge_milestone`, dependency verbs. *(First cut may land the service + read
  API + create/resolve/lifecycle; merge and dependencies can follow.)*
- **Events**: lifecycle transitions emit `milestone.created` / `.activated` /
  `.achieved` / `.cancelled` / `.merged` on the platform's signed event channel
  — co-designed with PM's durable-lifecycle-events work (pm#64); the registry
  transition is the fact PM's release flow (pm#68) records against.

## 6. Consumer contracts

### 6.1 Governance — spec validity across milestones

Amends `governance-plugin.md` §4/§5. The `ArtifactVersion.milestone` stamp
graduates from soft annotation to **resolution key**:

1. **Validated at mint.** `register_artifact` / `revise_artifact` resolve the
   milestone ref against the platform (context = the artifact's governing
   scope) and store the canonical address; unknown refs hard-fail. This
   supersedes the "stamped verbatim, never resolved" posture of #141.
2. **Pending leaves.** A version that supersedes the current leaf but is
   stamped with a **planned** milestone is *pending* — not competing, not
   canonical. Canonicality is no longer bare leaf-ness: **canonical = the
   newest version whose stamp is absent, active, or achieved**, computed by
   reading milestone state from the platform (the same consume-pattern as the
   scope tree). Drafting the v2 spec no longer dethrones the v1 spec.
3. **Per-milestone reads.** `get_artifact(..., milestone=REF)` returns the
   version valid *for* that milestone — its stamped version if one exists, else
   the canonical version. The default read is unchanged (canonical).
4. **Promotion is implicit.** When a milestone flips planned→active, its
   pending versions become canonical **by resolution** — no governance write,
   no cross-plugin choreography; the platform transition *is* the promotion.
5. **Cancellation.** Versions stamped with a cancelled milestone surface as
   dead pending branches (auditable, re-stampable via a fresh revision); the
   canonical version is unaffected. `resolve_artifact` keeps its role for
   genuine competing leaves *within* the same state bucket.

### 6.2 PM — validated tagging (private plugin; adoption tracked in snowline-pm)

- `set_milestone` / item-create **validate + resolve** via the platform, using
  the item's primary scope as bare-name context; store canonical. The original
  drift becomes inexpressible.
- `milestone_status` / `whats_next(milestone=)` resolve refs identically and
  surface the canonical matched address (closes the misread reported in
  feedback `489a30ff`).
- An item keeps **one** target milestone; a `required` | `optional` membership
  attribute (default required) is PM-side. Later milestones depend on earlier
  ones rather than re-counting foundation items.
- **Manual exit criteria are human-owned work items** (`set_human_owned`)
  tagged required to the milestone — no parallel checklist system;
  `milestone_status` stays one computation.
- Readiness reporting (required-remaining, blockers, scope churn — never a
  lone percentage) and roll-forward audit history (original milestone, new
  milestone, reason, actor) are PM-side over the registry.
- pm#68's release verb folds in: "release" = `achieve` on the registry plus
  PM's lifecycle event (pm#64). #68's "slug owned by nobody" posture is
  **superseded** by this registry — the drift evidence is what changed the
  call.

## 7. Merge + migration (pre-registry drift)

- `merge(from, into)` marks `from` as an **alias tombstone**: resolving it
  returns `into`, forever. The platform **never bulk-retags** plugin data (it
  cannot write plugin stores — the LLM is the integration runtime); retagging
  is agent-driven and optional, since aliased reads already agree.
- Known first customer: the `v1-launch` split above. Its reconciliation is
  **owner-manual by explicit decision** — the alias makes both addresses read
  as one milestone without touching a single item.
- Bootstrap: the registry is seeded by **explicit creates** from observed tags,
  not auto-imported — same no-vivify posture as everywhere else.

## 8. Out of scope

- Membership storage, required/optional, readiness computation, roll-forward
  history — PM's, over the registry.
- Artifact content/version mechanics beyond §6.1's resolution change.
- Release/deploy tracking — a milestone may name a target release in prose;
  releases are not modeled.
- Auto-achievement, progress percentages, and any scheduling authority:
  milestones must not become a second priority system — an explicit
  milestone *focus* filter on `whats_next` is PM-side and visibly stateful.

## 9. Implementation note

A new platform-owned table + service beside scopes (Postgres + Alembic, rides
replication like scope rows per `replication-continuity.md`; lifecycle events
on the platform's signed channel). The governance change is a resolution-layer
change plus stamp validation — the schema already carries `milestone` (#143).
The HTTP surface mirrors the scope read/resolve API. PM adoption is its own
work items on `snowlinedev/snowline-pm` (this spec is the contract they build
against).

## 10. Acceptance criteria

Adapted from the internationalization pressure test:

- Create `localization-foundation`, `spanish-beta`, `spanish-ga`, `rtl-ready`
  anchored at `turtlesedge/turtletracks`; beta depends on foundation, GA on
  beta, RTL on foundation; a cycle is rejected.
- `resolve("spanish-beta", context=turtlesedge/turtletracks)` returns the
  canonical address; a typo'd ref hard-fails with a near-miss suggestion and
  **nothing is minted**; with both `org/x` and `org/repo/x` registered, a bare
  `x` in repo context resolves to the repo anchor.
- Governance: revising a spec with a planned-milestone stamp leaves the old
  version canonical and the new one pending; `get_artifact(milestone=…)`
  returns the per-milestone version; activating the milestone flips canonical
  **with no governance write**; cancelling it surfaces the pending version as
  a dead branch and leaves canonical untouched.
- `merge(a, b)`: reads via either address agree; resolution of `a` returns `b`
  with the alias noted; no plugin rows were rewritten.
- `achieve` is explicit: all-items-terminal alone never achieves; cancelling a
  milestone cancels no initiative and no work items (asserted PM-side at
  adoption).
- Lifecycle transitions emit signed events a subscriber verifies with the SDK.
