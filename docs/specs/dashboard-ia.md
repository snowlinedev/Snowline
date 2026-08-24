# Dashboard information architecture — demand-side navigation & placement intent

> **Status: draft** (planning item `aa4bdd1e` / issue #181, milestone
> `snowlinedev/dashboard-ia-v1`). Extends `ui-shell.md` §3/§6: the UI-shell
> spec governs what a view IS (kinds, data plane); this spec governs WHERE
> views live and WHY. Registered as a governance artifact beside
> `dashboard/ACCESSIBILITY.md` so the IA evolves deliberately, never by
> accretion. Spec input: feedback thread `7d6d14a5` → `a16d9df0` → `303ac5a6`
> → `5f86311d` (2026-08-21/22).

## 1. Problem

The dashboard's styling is cohesive — tokens plus the governed
`ACCESSIBILITY.md` doing their job — but its organization is emergent, not
designed. The entire placement vocabulary today is two booleans' worth of
expressiveness:

- pages: `nav: true|false`, grouped **by plugin** in plugin-list order
  (`registry.ts::pluginNavGroups`);
- widgets: `slot: "home"` — a closed one-value enum appending cards to one
  flat grid in plugin-list order.

That is **supply-side** organization: the nav mirrors the plugin registry.
The owner's questions are **demand-side**: *what needs me today? what's
queued? how is feature X doing across repos? what shipped? what's waiting on
my judgment?* With ~13 registered contributions across two plugins already
(pm: 7 pages + 1 widget; governance: 2 pages + 2 widgets), every new plugin
makes the mismatch worse: nobody's work item is "compose these into an
answer," so the dashboard accretes islands.

The founding requirement of the UI shell was ONE product. §2 of `ui-shell.md`
bought visual unification by construction; this spec buys **navigational**
unification the same way: plugins declare what a view is *for*, the platform
owns where it lives.

## 2. Principles

1. **Demand-side IA.** Top-level navigation mirrors the owner's questions,
   never the plugin registry. Plugin identity is diagnostic detail (it stays
   on cards and in System), not an organizing axis.
2. **Plugins declare intent, the platform places.** A plugin says "this is a
   needs-attention view"; only the platform knows that Today's first band
   composes attention views. Plugins cannot name sections, order themselves,
   or squat on prime placement — the same posture as kinds: plugins ship
   JSON, the shell owns every pixel.
3. **Composition is layout, not synthesis.** The platform arranges
   contributed views into sections and bands; it never merges, joins, or
   interprets their data (thin-server posture, `ui-shell.md` §2 unchanged).
4. **Fail visible, degrade to today.** An unknown intent — or none — lands a
   contribution in the plugin-grouped fallback exactly where it lives now.
   New vocabulary never bricks registration and never silently drops a view.

## 3. The IA: five sections

Top-level nav, in order. Each section is defined by the question it answers,
not by which plugin feeds it.

| Section | Route | The owner's question | Composed from |
|---|---|---|---|
| **Today** | `/` | What needs me right now? What just happened? | intents `attention`, `digest`, `activity` (§5) |
| **Roadmap** | `/roadmap` | What's queued and in flight, per scope? | intent `roadmap` (pm's board/scope pages are the founding tenants) |
| **Features** | `/features` | How is feature X doing across repos? | intent `feature-status` (milestone-umbrella views; milestone slug is the cross-plugin correlation key, decision `49e02e29`) |
| **Review** | `/review` | What's waiting on my judgment? | intent `review-queue` (feedback, triage, drift observations, shadow branches) |
| **System** | `/system` | Is the machine healthy? | native Plugins / Surfaces / Scopes views + intent `admin` |

Rules:

- **Today replaces Home at `/`.** The current Home grid's native cards
  (plugin health stat, plugin status, surfaces) move to System; a compact
  health indicator may remain in the shell chrome, but Today's content is
  the owner's work, not the machine's.
- **Sections are platform-owned and fixed.** Adding a section is a platform
  PR revising this artifact — the same bar as adding a kind. Plugins can no
  more invent a section than they can ship JavaScript.
- **A section with nothing to show renders an explicit empty state**, never
  disappears — a vanishing nav entry reads as breakage (and violates the
  stable-chrome expectation ACCESSIBILITY.md's keyboard tests pin).
- **The fallback group survives.** Registered `nav: true` pages with no
  recognized intent appear grouped under their plugin name after the five
  sections — exactly today's behavior, demoted from being the whole IA to
  being the escape hatch.

## 4. The placement-intent contract

`UIPage` and `UIWidget` (manifest `ui` block, `ui-shell.md` §3) each grow one
optional field:

```jsonc
"pages": [
  { "id": "roadmap", "route": "/roadmap", "kind": "board",
    "data": "/ui-api/pages/roadmap",
    "intent": "roadmap" }                     // ← new
],
"widgets": [
  { "id": "needs-you", "slot": "home", "kind": "list",
    "data": "/ui-api/widgets/needs-you",
    "intent": "attention" }                   // ← new
]
```

### 4.1 Intent vocabulary (v1)

| Intent | Declares the view is… | Platform places it |
|---|---|---|
| `attention` | a needs-you / blocked / overdue surface | Today, first band |
| `digest` | a summary of state ("since you last looked") | Today, second band |
| `activity` | a recent-events feed (shipped, decided, changed) | Today, third band |
| `roadmap` | planning structure (what's queued, in what order) | Roadmap |
| `feature-status` | cross-scope progress on a named body of work | Features |
| `review-queue` | items awaiting explicit human judgment | Review |
| `detail` | an entity page reached by links, never browsed to | no nav entry (subsumes `nav: false`) |
| `admin` | an operational / registry / diagnostic view | System |

### 4.2 Validation & compatibility posture

- **`intent` is a free string, fail-visible** — the same treatment as `kind`
  (`ui-shell.md` §3): an unknown value registers fine and degrades to the §3
  fallback group at composition time. The vocabulary is shell-version-
  dependent; a newer plugin against an older shell must degrade to today's
  layout, not brick registration.
- **Additive, no `contract_version` bump.** The field is optional with
  today's behavior as its absence-semantics, so existing v1 blocks are
  untouched. Ordering constraint (first-party fleet): the platform ships and
  deploys the field **before** any plugin declares it — a plugin sending
  `intent` to an older platform is rejected 422 by `extra="forbid"`. That is
  the documented cost of fail-loud manifest validation; it is paid once, at
  rollout, by deploy order.
- **`intent` and `nav` interact predictably.** When `intent` is present it
  fully determines placement and `nav` is ignored; absent `intent`, `nav`
  keeps its current meaning. `detail` and `nav: false` are equivalent;
  declaring both is harmless.
- **Widgets keep `slot: "home"`** as dead-but-valid vocabulary: a widget with
  an intent is placed by intent; a widget with only `slot: "home"` lands in
  Today's fallback band ("More from *plugin*"). Widening or retiring `slot`
  is deferred until a second slot would actually exist.
- **Ordering within a band** is plugin-registration order, as today. A
  `weight` hint is explicitly deferred (§9) — no plugin self-promotion until
  a real composition conflict exists.
- **Three schema copies, one drift guard.** The field lands in
  `snowline_platform.manifest` (source of truth), is mirrored in
  `snowline_plugin_sdk.ui` (pinned by `test_ui_contract_drift.py` — the new
  field constant must join the pinned sets), and typed in
  `dashboard/src/api.ts`. Composition logic (intent → section) lives in
  `dashboard/src/registry.ts` beside `pluginNavGroups`, which it subsumes.

## 5. Today: banded composition

Today is the briefing surface — the page the owner opens first, and where the
planned daily audit digest lands. It is composed, not synthesized:

- **Bands, in order: Needs you** (`attention`), **Digest** (`digest`),
  **Recent activity** (`activity`), then the fallback band for intent-less
  home widgets. A band with no contributions (or all-empty ones) collapses.
- **Each contribution stays a self-polling card** (`useData` per card, as the
  Home grid does now) — one dead plugin cannot blank the page, and a DOWN
  plugin's cards grey out via the same health state as everywhere else.
- **The platform contributes no synthesis.** "Needs you" is N cards from N
  plugins, visually banded; it is not a merged list. If a merged portfolio
  view is ever wanted, that is a plugin's job to serve as data (pm's
  briefing already computes exactly this — it needs only `/ui-api` exposure
  and an `attention`/`digest` declaration to become Today's founding
  tenant).

## 6. Permalinks

Every entity detail page (`intent: detail`) is a stable, shareable URL —
plugin-namespaced routes already guarantee uniqueness; this spec makes
stability a requirement rather than an accident. Rationale: agents cite
dashboard URLs in session output and work-item bodies (feedback `4bd3e4d3`
— the GitHub mirror is currently the only followable citation for a human).
Renaming a route is a breaking change to be called out in review.

## 7. Accessibility

`dashboard/ACCESSIBILITY.md` governs unchanged. IA-specific obligations:

- The five sections are ordinary nav links: keyboard-reachable, current-page
  marked, per-route document titles (2.4.2) — `tests/keyboard.test.tsx`
  extends to the new routes.
- Band headings on Today are real headings (landmark/heading structure, not
  styled divs), so the page outline mirrors the visual bands.
- The <640px nav disclosure pattern is unchanged; five sections + fallback
  groups must remain operable inside it.

## 8. Build order

1. **Contract** (platform): `intent` on `UIPage`/`UIWidget` in `manifest.py`,
   SDK mirror + drift-guard update, `api.ts` types. No rendering change.
2. **Shell IA skeleton** (platform): five-section nav + routes, intent →
   section composition in `registry.ts`, fallback group, System absorbs the
   native pages, empty states, keyboard/title test extensions.
3. **Today** (platform): banded composition at `/`, native Home cards move
   to System.
4. **PM briefing to the browser** (snowline-pm): `/ui-api` endpoints over the
   existing `briefing.py` (needs-you, digest, recent completions) + widget
   declarations with `attention`/`digest`/`activity` intents.
5. **Adoption** (snowline-pm, governance): intents on existing registrations
   — roadmap pages → `roadmap`, milestone pages → `feature-status`, shadow
   branches → `review-queue`, detail pages → `detail`.
6. **Review section tenants** (snowline-pm): feedback + triage queues get
   `/ui-api` views declared `review-queue` (both are MCP-only today).

Each step is independently shippable; after step 2 the dashboard is already
demand-side-shaped with existing contributions in the fallback groups, and
each adoption step moves views into their sections.

## 9. Out of scope (this increment)

- **`weight`/ordering hints** — plugin-registration order until a real
  conflict exists.
- **User-customizable layout** (pinning, hiding, reordering) — no persisted
  per-user UI state, consistent with `ui-shell.md` §9.
- **Cross-plugin data merging** — composition stays layout-only (§2.3).
- **Multi-milestone membership** (feedback `75c7e43a`) — Features renders
  what `milestone_status` can answer today; single-tag items under-report
  cross-axis membership. PM-side fix, tracked there; Features inherits it
  for free when it lands.
- **A second widget `slot`** — revisit when a section needs widget-vs-page
  distinction beyond Today.
