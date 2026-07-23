# Governance plugin

> **Status: draft.** The functional contract for Snowline's first plugin. It
> describes EXPECTED FUNCTIONALITY, not extraction mechanics — the implementation
> route happens to be a carve from the frozen monolith (`snowlinedev/snowline-v3`),
> but this spec is what the plugin must DO and could be handed to a fresh agent.

## 1. Purpose

Governance is Snowline's **durable, cross-session memory of reasoning**: the
**decision graph** (what was decided, why, and how decisions supersede each
other), a **shadow/speculation graph** (explore rival directions in isolation
from the real graph until promoted), and **specs/artifacts** (governing
documents with versions, maturity, and scope-applicability). It is the flagship
capability the platform showcases.

Governance does **not** own scopes — the **platform** owns the scope namespace
(see §3). Governance *references* scopes and reads the platform's scope tree to
compute applicability.

## 2. Place in the platform

Governance is a plugin: an out-of-process module the platform's gateway composes
and the supervisor health-checks. It:

- **Registers** with the platform via a manifest declaring its `base_url`, its
  MCP surfaces, its health endpoint, and its scope dependency.
- **Maps two MCP surfaces** onto platform named-surfaces (decision `70b415fd`):
  - `main` — the real-write decision + artifact tools and the read tools.
  - `shadow` — the speculation write tools + read-real grounding, **no
    real-write** (the isolation property, decision `8a7f0a11`). Mirrors the
    monolith's `/shadow/mcp`.
- **Emits decision events** on the platform's webhook channel (§7).
- **Has a UI** (a later increment): the decision/scope/spec views and a
  *separately-mounted* shadow view (UX isolation mirrors the MCP isolation).

## 3. Dependency on the platform scope namespace

The platform owns scopes — the `Scope` identity + the `parent_id` tree + the
`isolated` flag (decision `61e50214`). Governance:

- Stores a **scope reference** (slug/id) on every decision and artifact-governs
  edge — a soft reference, not a cross-service FK.
- Reads the platform's **scope tree** to compute ancestor-inherited applicability
  (§6.1). This requires a platform-provided **scope read/resolve surface**.

> **Prerequisite / sequencing:** governance's applicability behavior depends on
> the platform scope service existing. That service (platform-owned) should be
> built before — or alongside — this plugin.

## 4. Data model (schema-compatible with the monolith)

Carried over unchanged in shape (a lift, per the develop-in-public carve), minus
`Scope` (which is the platform's):

- **Decision** — `id`, `scope` (ref), `decision` (statement), `rationale`,
  `recorded_at`, `supersedes_id` (self-FK, **non-unique** → a branching
  supersession DAG, not a single chain), plus shadow-graduation provenance
  fields. Decisions FK only to their scope ref and to other decisions.
- **Shadow graph** — `ShadowBranch` (named, scope-anchored speculation lines
  with running narrative notes), `ShadowNode` (speculative decisions + rationale),
  `ShadowNodeCitation` (inward-only: a node may cite another node in its own
  branch, or a real decision — never the reverse), `ShadowConversationEvent`
  (durable, resumable per-branch conversation log).
- **Artifacts** — `Artifact` (a governing doc: `doc_kind` spec/plan/reference,
  `backend` git|inline, `maturity` draft→exploratory→stable, `governs_all`),
  `ArtifactVersion` (supersession DAG + content snapshot/locator, plus an
  optional `milestone` — a SOFT release-correlation slug stamped verbatim at
  mint, never resolved, the portfolio's cross-plugin key: PM tags work items
  with the same slug, so a stamped version records which artifact version a
  release shipped as; grammar-validated + canonical-lowercase like a scope slug,
  case-insensitive input per #139.
  **Amended for first-class milestones** (`milestones.md` §6.1): the stamp
  graduates from soft annotation to resolution key — validated against the
  platform milestone registry at mint, and a leaf stamped with a *planned*
  milestone is *pending*, not canonical/competing, until its milestone goes
  active),
  `ArtifactGoverns` (artifact↔scope, multi-scope).
- **Webhook bus** — `WebhookSubscription`, `WebhookDelivery` (§7).

## 5. MCP surfaces

### `main` surface
- **Decisions (write):** `record_decision`, `supersede_decision`.
- **Decisions (read):** `get_decision`, `list_decisions`, `applicable_decisions`
  (ancestor-inherited — §6.1).
- **Artifacts (write):** `register_artifact`, `revise_artifact`
  (both accept an optional `milestone` release slug stamped on the version),
  `resolve_artifact` (leaf resolution), `set_governs`, `set_maturity`.
- **Artifacts (read):** `get_artifact` (the full record — `current_version`
  carries the canonical inline body by default; `include_body=False` for the
  lean header), `get_artifact_version` (one version's body by (artifact,
  version) pair — competing leaves for branch comparison, superseded versions
  for audit/pinned reads), `list_artifacts` (lean headers only),
  `list_artifact_versions` (versions across all artifacts stamped with a given
  `milestone` slug — the release-correlation read). Every version read surfaces
  its `milestone`.
- **Scope reads:** delegated to / proxied from the platform scope surface (a
  reader needs the tree to make sense of inheritance).

### `shadow` surface (isolation by construction)
- **Shadow write:** `create_branch`, `list_branches`, `get_branch`,
  `set_narrative_notes`, `add_node`, `add_citation`, `list_citations`,
  `shadow_corpus_search`.
- **Read-real grounding:** the read tools above (`get_decision`,
  `list_decisions`, `applicable_decisions`, artifact reads, scope reads) so a
  speculation agent can ground in the real graph.
- **Absent by construction:** every real-write verb (`record_decision`,
  `supersede_decision`, the artifact writes). This absence IS the isolation
  guarantee — a speculation session physically cannot mutate the real graph.

## 6. Behaviors (the functional contract)

1. **Ancestor-inherited applicability** (`applicable_decisions`, decision
   `ee999c8d`): from a reader at scope X, applicable decisions = X's own current
   leaves PLUS every ancestor's, walking the platform's `parent_id` tree UPWARD
   and **halting at the first `isolated` scope** and the forest root. Each
   inherited row carries `from_scope`. The same isolation-aware walk governs
   artifact `governs`-matching. `parent_id` edges are authoritative (`4440aca5`).
2. **Supersession / leaves:** a "current" decision is a leaf of the supersession
   DAG (nothing supersedes it); supersession is intra-scope. Two decisions may
   supersede one (a branch); leaves surface without inference.
3. **Artifacts:** `governs` accepts one slug, a list, or `*` (all scopes);
   maturity is a descriptor ladder (not a gate); content resolves via a backend
   adapter — `git` (repo doc, sha-pinnable) or `inline` (content in the
   substrate). `resolve_artifact` collapses competing version leaves.
4. **Shadow isolation + graduation:** speculation is invisible to the real graph
   until explicitly graduated; graduation translates a shadow node into the real
   primitives it implies (`record_decision` + `set_governs`), agent-curated,
   human-ratified. Archive may carry a rejection-decision facet.

## 7. Events — the webhook channel

Governance emits `decision.recorded` / `decision.superseded` on a signed webhook
bus (HMAC-SHA256 over the raw body; `contract_version` in the payload; a
transactional outbox + async delivery with per-subscription monotonic `seq`).
Event-type registry + `CONTRACT_VERSION` are the published contract (the
`snowline-plugin-sdk` consumes them). Subscribers are other plugins/services.

> **Amended for replication** (`replication-continuity.md` §3.2, #77): the
> per-subscription delivery-time `seq` above (decision `97907576`, #630) is the
> fire-and-forget webhook shape only. Replication streams use the SDK's
> `snowline_plugin_sdk.replication` modules — `seq` allocated at EMIT time in
> the domain write's transaction, streams keyed `(source_id, epoch)`,
> `peer_seen` causal context, contract version 2 — which governance adopts in
> replication-continuity §9 item 3 (#79). Signatures stay delivery-time over
> the exact bytes POSTed in both classes.

## 8. Out of scope (these are other plugins / the platform)

- The PM layer — work items, initiatives, phases, roadmap, `whats_next`,
  briefing, triage, recurring work, task sinks.
- GitHub / Todoist integration and reconcile.
- The drift / triage **carriers** (separate carrier plugins).
- **Scopes** — owned by the platform, not governance.

## 9. Implementation note (carve, not redesign)

Built by carving the already-clean governance modules from the frozen monolith
as read-only reference — `decisions`, `shadow`, `branching`, `replication`
(EMIT), `replication_ingest`, the substrate-core models (`models_core` minus
`Scope`), the artifact layer, and the `snowline-plugin-sdk` — and writing the
de-PM'd versions of the entangled bits fresh (the MCP surfaces split main/shadow
per the monolith's pattern; an inline-capable `artifacts` without the
`scope_config`/git-PM edges; scopes referenced from the platform, not owned).
Schema stays compatible with the monolith so existing decisions/scopes can be
migrated into the running instance later.

## 10. Acceptance criteria

- Registers with the platform and exposes `main` + `shadow` surfaces; the gateway
  composes them; `record_decision` is provably **absent** from `shadow`.
- `applicable_decisions` returns own + ancestor-inherited decisions, halting at
  `isolated`, tagged with `from_scope`, reading the platform scope tree —
  verified against production-faithful data.
- Decision supersession, artifact governs/maturity/resolve, and shadow
  create/add/cite/graduate all work, with tests.
- Emits signed, versioned `decision.*` events a subscriber can verify with the
  SDK.
- Imports no PM code (import-pure); runs against a substrate-only DB.
