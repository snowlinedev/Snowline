# UI shell — declarative widget & page composition

> **Status: exploratory** (phase 1 shipped — PR #56; governance artifact
> `945a36ee`). How Snowline gets a unified web UI: a single
> platform-owned dashboard shell that renders **declaratively registered**
> widgets and pages from plugins. Plugins ship JSON, never JavaScript. The
> composition model deliberately mirrors the MCP gateway (`gateway.md`): plugins
> register capabilities as data on the existing manifest/heartbeat lifecycle;
> the platform composes and presents. Decided in session 2026-07-03 (Sean);
> the rejected alternative is recorded in §2.

## 1. Purpose

Give Snowline one browser surface — served by the platform, gated by the same
tailnet trust edge as everything else — that shows:

- **Platform-native views**: the plugin registry (status, heartbeat freshness,
  manifests), the mounted gateway surfaces + allowlists, and the scope tree.
- **Plugin-contributed views**, starting with governance's **shadow decision
  discussions** (branch list → discussion thread with citations), later the PM
  roadmap.

The UI must look and behave as ONE product ("somewhat unified looking" is the
founding requirement), be WCAG 2.2 AA by default (§7), and stay useful over
both `localhost` and the tailnet IP with zero per-client config (the
SSH-into-host daily flow).

## 2. The model: declarative registration, platform-owned rendering

**Decision.** Plugins register widgets and pages as *data* — a `ui` block on
their existing manifest — where every widget/page names a platform-defined
**kind** and a plugin JSON endpoint that feeds it. Exactly one frontend exists:
the platform shell, a React app owning the router, nav, theme, density, and a
component library implementing the kind vocabulary (§4). Plugins never ship
JavaScript.

What this buys, by construction rather than by convention:

- **Unification.** One design system, one bundle, one accessibility/density
  implementation (§7) that plugins cannot opt out of, half-follow, or drift
  from.
- **Lifecycle for free.** The `ui` block rides `POST /plugins` and the
  registration heartbeat (issue #39/#49): a redeploy with new widgets takes
  effect within one beat (the upsert `updated` path), a platform restart
  recomposes the UI when plugins re-register, and a DOWN plugin's
  contributions grey out via the same health status the gateway routes on. No
  second registry, no second lifecycle.
- **Thin-server consistency.** Like MCP tools, UI contributions are structured
  surfaces over the plugin's own store; the platform does no cross-plugin
  synthesis.

**Rejected: proxied plugin SPAs / remote modules (micro-frontends).** Each
plugin shipping its own UI (whole app behind a `/ui/<plugin>` proxy, or JS
modules built against a shared kit and mounted by the shell) maximizes
expressive power but makes "unified" a discipline problem and introduces a
shell↔plugin bundle version-skew surface — a runtime drift class we
deliberately engineer away elsewhere. Remote modules remain the **documented
escape hatch** if a future view genuinely exceeds the kind vocabulary
(candidates: rich editors, canvas/graph visualizations); adding a kind to the
platform is the first resort while all plugins are first-party.

## 3. Registration: the manifest `ui` block

`PluginManifest` grows an optional `ui` object:

```jsonc
"ui": {
  "contract_version": 1,
  "widgets": [
    {
      "id": "shadow-activity",          // unique within the plugin
      "slot": "home",                    // v1: only "home" (the dashboard grid)
      "kind": "stat",                    // §4 vocabulary
      "title": "Open shadow branches",
      "data": "/ui-api/widgets/shadow-activity",  // plugin-relative path
      "refresh_seconds": 30              // shell polling hint; shell may clamp
    }
  ],
  "pages": [
    {
      "id": "shadow-branches",
      "route": "/shadow",                // shell route, namespaced: /governance/shadow
      "title": "Shadow discussions",
      "nav": true,                       // appears in the shell nav
      "kind": "table",
      "data": "/ui-api/pages/branches"
    },
    {
      "id": "shadow-branch",
      "route": "/shadow/{branch}",       // path params template into `data`
      "nav": false,                      // reached by row links, not nav
      "kind": "thread",
      "data": "/ui-api/pages/branches/{branch}"
    }
  ]
}
```

Rules:

- **Routes are plugin-namespaced by the shell** (`/<plugin>/<route>`), so
  plugins cannot collide or squat on platform-native routes. Path params in
  `route` template verbatim into `data`.
- **`data` paths are plugin-relative** and fetched through the platform proxy
  (§5). A plugin cannot point a widget at another plugin or an external host.
- **Validation at registration**: unknown top-level fields, duplicate ids, or
  malformed routes reject the manifest (422) — same fail-loud posture as
  `SNOWLINE_SURFACE_PLUGINS`. An unknown `kind`, however, registers fine and
  **fails visible** at render (§4.4) — kinds are shell-version-dependent, and
  a newer plugin against an older shell must degrade loudly, not brick
  registration.
- **`contract_version`** guards the block's shape (not the kind vocabulary):
  the shell renders versions it knows and shows the §4.4 placeholder per
  contribution otherwise. The schema lives in the plugin SDK next to the
  manifest shape, with the same drift-guard treatment as the contract
  constants.

## 4. Kind vocabulary (v1)

Each kind = a JSON response contract (plugin side) + a shell component
(platform side). Sized for shadow-now / roadmap-later; grow it by platform PR.

### 4.1 Widget kinds (home grid)

| Kind | Contract (response body) | Renders as |
|---|---|---|
| `stat` | `{ value, label?, delta?, intent? }` | number tile with optional trend/intent color |
| `list` | `{ items: [{ text, href?, meta?, intent? }], empty? }` | short linked list (shell caps length) |

Platform-native widgets (plugin health, surface summary) use the same kinds
internally — the platform eats its own vocabulary.

### 4.2 Page kinds

| Kind | Contract (sketch) | Notes |
|---|---|---|
| `table` | `{ columns: [{ key, label, kind? }], rows: [{ cells, href? }], empty? }` | column `kind` hints (text/chip/time/actor); row `href` targets shell routes |
| `thread` | `{ title, meta, nodes: [{ author, kind, markdown, at, citations: [...] }] }` | the shadow discussion view: ordered authored markdown nodes with metadata |
| `document` | `{ title, markdown, meta? }` | rendered markdown (e.g. branch narrative notes) |
| `board` | `{ nodes: [BoardNode...], group_by?, facets?, empty? }` (§4.2a) | hierarchical, collapsible, read-only tree — specified for the pm roadmap (`roadmap-board.md`, snowline-pm repo) |

`search` (query box + result list, for shadow corpus search) is anticipated
but **deferred** until the read views prove out.

#### 4.2a The `board` kind — hierarchical read view (specified — roadmap-board.md)

`table` renders one flat grid; the pm roadmap needs REAL nesting (initiative →
phase → item), per-node status badges, small progress indicators, and a couple
of client-side view toggles (group-by, show/hide facets) that must not
round-trip the network on every click. `board` is that kind — still fully
declarative (the plugin ships JSON, the shell owns every pixel, §2's posture
holds), but it is the FIRST kind where the shell keeps a small amount of
client-side view state (which facets are hidden, flat vs grouped) over one
already-fetched payload, rather than re-fetching per view change. That's a
deliberate, narrow exception, not a precedent for shell-side business logic:
the shell does not know what "org" or "stale" MEAN — it only groups nodes by
a `group_by.key` the plugin names and hides nodes an already-plugin-stamped
boolean flags, exactly the same "plugin computes meaning, shell only renders"
split every other kind holds. This is viable because board data is small
(a portfolio's roadmap, not a paginated log) — a kind for large/streaming
hierarchies would need a different contract.

**`BoardNode`** (recursive):

| field | req? | meaning |
|---|---|---|
| `id` | yes | stable id, unique within the whole tree — the React key and (with `href`) the drill-down target |
| `label` | yes | the node's primary text |
| `href` | no | a shell route this node links to (same plugin-relative + re-prefix treatment as a `table` row/`list` item `href`) |
| `kind` | no | a free string node-type hint (e.g. `initiative`/`phase`/`item`) — styling/iconography only, the shell does not branch behavior on it |
| `meta` | no | small trailing text (e.g. an age like `"2d"`) |
| `chip` | no | small leading text badge distinct from `badges[]` (e.g. a scope slug) — one visually-secondary chip, not a list |
| `badges` | no | `[{ text, intent? }]` — status chips (e.g. `{"text": "STUCK", "intent": "bad"}`); `intent` reuses the `stat`/`list` vocabulary (`good`\|`bad`\|`neutral`\|string) and is NEVER the only signal (§7: intent is a decoration alongside visible text, same rule `KindList`'s `dot` already follows) |
| `annotation` | no | one line of small explanatory text under the label (e.g. `"waiting on Downgrade flow PR"`) — plain text, not markdown (no parser needed for a one-liner; a plugin wanting rich text uses `document` instead) |
| `progress` | no | `{ segments: [{ status }], complete, total }` — `segments` is an ordered list of per-phase-like status strings (`"complete"`\|`"active"`\|`"upcoming"`, open string set — an unknown value renders as `upcoming`'s neutral style rather than erroring) rendered as a small dot row; `complete`/`total` render alongside as `"2/5"` |
| `group_key` | no | which top-level group this node belongs to when `group_by` is set (§ below) — meaningless on non-top-level nodes, ignored there |
| `facets` | no | `{ [facetKey]: bool }` — which of the payload's declared `facets[]` this node satisfies (e.g. `{"stale": true}`); a facet a node doesn't mention defaults to `false` |
| `collapsed_by_default` | no | default `false`; a node with children starts collapsed if `true` — expand/collapse is local shell state, never persisted server-side or round-tripped |
| `children` | no | `BoardNode[]`, same shape, recursively — omitted/empty on a leaf |

**Top-level response:**

```jsonc
{
  "nodes": [ /* BoardNode[], already in the plugin's intended default order */ ],
  "group_by": {                      // optional — omit to offer no grouping toggle
    "key": "group_key",
    "label": "org",                  // the toggle's visible label, e.g. "By org"
    "flat_label": "Flat"             // the ungrouped toggle's visible label
  },
  "facets": [                        // optional — omit to offer no filter toggles
    { "key": "stale", "label": "Hide stale scopes", "hidden_by_default": true },
    { "key": "initiative_only", "label": "Initiative work only", "hidden_by_default": false }
  ],
  "empty": "Nothing here."
}
```

**Shell rendering.** Top-level nodes render as a numbered, collapsible list
(node index, 1-based — pure presentation, never a payload field, so a
plugin's own ordering is always what's numbered). A node with `children`
renders an expand/collapse control (default state per
`collapsed_by_default`); collapsing hides the whole subtree, purely a CSS/DOM
state change — no refetch. When `group_by` is present the shell offers a
`flat_label`/`label` toggle (radio-style, `Flat` selected by default) that
either renders `nodes` as-is or buckets top-level nodes by `group_key` under a
group heading (nodes with no matching `group_key` fall into an "Ungrouped"
bucket) — same node array, same objects, just a different top-level grouping,
so this NEVER changes which nodes exist or their `href`s. When `facets` is
present the shell renders one toggle per declared facet; a facet whose
`hidden_by_default` is `true` starts filtering nodes where
`node.facets[key] === true` OUT of view (both the "hide" wording and the
default read the same way: the facet names the thing being HIDDEN, matching
the pm mock's own "Hide stale scopes" copy) — toggling a facet OFF/ON never
refetches, and a parent whose every child is filtered out still renders
(collapsed to show 0 visible children, not hidden itself) so the tree's shape
stays legible.

**Validation** (fail-visible, §4.4, same posture as every other kind): a
response missing `nodes` (or `nodes` not an array), or any node missing
`id`/`label`, renders the malformed-data card. `group_by`/`facets`/per-node
optional fields are never validated beyond their own type — a facet key with
no `facets[]` declaration still renders (just with no toggle to hide it), and
an undeclared `group_by.key` degrades every node into the "Ungrouped" bucket
when grouped view is selected, never an error.

**Deferred, this increment:** drag-to-reorder (§4.3 already names this as a
later `board` capability — a reorder endpoint + capability flag, once a
plugin needs write-from-board); `search`-style free-text filtering (facets are
plugin-declared booleans, not an open query); persisting a user's toggle
choices across sessions (local component state only, resets on reload,
consistent with §9's "no persistence beyond the tailnet trust edge" posture
for v1 UI state generally).

### 4.3 Actions (page-level write affordances — specified, §5)

A page may declare **actions** — labelled buttons that open a minimal form and
POST it through the §5 proxy; the plugin owns all semantics. The full contract
(fields, response, validation) is in §5; the shape at a glance:

```jsonc
"actions": [{
  "id": "new-branch",
  "label": "New branch",
  "endpoint": "/ui-api/pages/branches",
  "fields": [
    { "name": "scope", "label": "Scope", "kind": "scope", "required": true },
    { "name": "name",  "label": "Branch name", "kind": "text", "required": true },
    { "name": "opening_message", "label": "Opening note", "kind": "multiline" }
  ]
}]
```

The seam has two activations, sharing the same proxy-POST enablement and
endpoint-allowlist posture (§5) but different shapes:

- **input-shaped** — the `thread` kind's **`composer`** (`shadow-conversations.md`
  §4): one declared POST endpoint rendered as a message box.
- **button/form-shaped** — page **`actions[]`** (this section; first activated by
  issue #123's "New branch"): a button opening a form of declared fields. The
  shell renders these GENERICALLY — a plugin declares label + endpoint + fields
  and gets rendering, submission, and success-navigation with no shell code of
  its own (the posture §2 requires).

Row/thread-node actions and reorderable tables/boards (drag-to-reorder as a
`table`/`board` capability flag + reorder endpoint) are the same idea at finer
grain and land with the pm work; page-level actions[] is the shape specified
now.

### 4.4 Fail visible

A contribution whose `kind` (or `contract_version`) the shell doesn't support
renders a placeholder card — "*governance offers a view this platform version
can't render*" — never a silent drop. A malformed data response renders an
error card with the plugin name and path. Same philosophy as fail-loud config:
the dangerous failure is the invisible one.

## 5. Data plane: the `/ui-api` proxy

The shell fetches all plugin data through the platform:

```
GET /ui-api/<plugin>/<path>   →   <plugin base_url><path>
```

- **One origin, one trust edge.** The browser only talks to the platform; the
  CIDR gate covers everything; no CORS anywhere; identical over localhost and
  tailnet.
- **Path allowlist:** only paths under `/ui-api/` on the plugin are proxied
  (the manifest `data`/`endpoint` values must start with `/ui-api/`), so the
  proxy cannot be aimed at a plugin's MCP surface or arbitrary routes.
- **Health-aware:** a DOWN plugin short-circuits to 503 (shell renders the
  grey state) instead of hanging on a dead upstream.
- **Verbs:** GET, plus POST to endpoints a plugin DECLARED as write targets
  in its `ui` block (`composer.endpoint`, `actions[].endpoint`) — structural
  allowlist, JSON-only, size-capped (`shadow-conversations.md` §3). Undeclared
  paths 403. Both declared-write flavors flatten into one allowlist the proxy
  matches uniformly: it admits a POST because *some* manifest field declared
  the concrete path, not because of which flavor did.

#### 5.1 The `actions[]` contract (specified — issue #123)

A page's `actions` list moves here from "reserved" (§4.3 previously sketched it
speculatively). Each entry is a page-level write affordance the shell renders
GENERICALLY — no plugin-specific UI code, per §2's posture. The producer↔
consumer field vocabulary is drift-guarded the same way the kind vocabulary and
`composer` are: `snowline_platform.manifest` (`ACTION_FIELDS`,
`ACTION_FIELD_FIELDS`, `ACTION_FIELD_KINDS`, pinned to the real
`UIAction`/`UIActionField` models) is the source of truth, mirrored in
`snowline_plugin_sdk.ui` and pinned equal by `test_ui_contract_drift.py`.

An `actions[]` entry:

| field | req? | meaning |
|---|---|---|
| `id` | yes | unique within the page's actions |
| `label` | yes | the button text |
| `endpoint` | yes | plugin-relative POST target, `/ui-api/`-confined + allowlisted exactly like `composer.endpoint`; may template `{param}` segments that must appear in the page `route` |
| `fields` | no | the form the shell renders (default `[]` = a bare button posting an empty body) |

Each `fields[]` entry — the field-shape table:

| field | req? | meaning |
|---|---|---|
| `name` | yes | the JSON key the shell submits this field's value as |
| `label` | no | visible field label (defaults to `name`) |
| `kind` | no | rendering hint: `text` (single line, default), `multiline` (textarea), or `scope` (a text input backed by a native `<datalist>` typeahead over the platform's scope slugs — assistance only, free text still allowed, and a failed/loading scope fetch degrades silently to a plain text input). A FREE string — an unknown value falls back to a text control at render, it does not reject the manifest. An older shell that predates a given kind rendering it as a plain text input is CORRECT degradation, not a bug |
| `required` | no | the shell blocks submit until this is non-blank (default `false`) |

**Shell rendering.** A `label` button sits above the page's kind content;
clicking it opens a form of the declared fields (text→input, multiline→
textarea, scope→input + native `<datalist>` typeahead over the scope slugs).
Submit POSTs `{ <name>: <value>, … }` as JSON through the proxy to
`endpoint`. `endpoint`'s `{param}` segments are route-templated the same way a
page's `data`/`composer` are, so an action on a `{param}` route reaches the
right concrete path.

**Response contract.** A 2xx body MAY carry `navigate` — a plugin-relative
shell href the shell lands on after a successful submit (re-prefixed with
`/<plugin>`, same as a table row `href`). Everything else in the body is
ignored by the generic shell. Absent `navigate` = the shell just closes the
form. This is how "New branch" (issue #123) lands the user on the newly-created
branch's thread page while the shell stays ignorant of branches.

**Registration validation** (`manifest.py`/`UIAction`): 422 on an `endpoint`
not under `/ui-api/`, an `endpoint` `{param}` absent from the page `route`, an
unknown action/field key (`extra="forbid"`), or a duplicate action `id` / field
`name` within a page. `actions` is valid on ANY page kind (unlike the
thread-only `composer`).

## 6. Platform-native views & the shell

Built into the shell, not registered (the platform is not a plugin):

- **Home**: the widget grid — platform-native widgets first (plugin
  registry/health, surfaces), then registered widgets grouped by plugin.
- **Plugins**: registry detail — manifest, status, surfaces, heartbeat
  freshness; the §4.4 states link here for diagnosis.
- **Surfaces**: mounted surfaces, allowlists, composed tool counts.
- **Scopes**: the scope tree.

Nav = native views + registered `nav: true` pages grouped under their plugin
name. Serving: the shell is a static Vite/React/TS bundle served by the
platform app; deploy follows the existing LaunchAgent build-and-serve pattern
and is verified with `npm run build` (not `tsc --noEmit`).

## 7. Accessibility & density

**Conformance target: WCAG 2.2 AA for the default presentation.** This ships
as `ACCESSIBILITY.md` — a **registered governance artifact** (scope
`snowlinedev/snowline`, `governs` the dashboard path) so the conformance claim
changes only via `revise_artifact` with rationale, never a drive-by edit. The
project is intended for open-source release; the claim must be auditable.

**Compact mode** is an explicit, persisted, user-selected density preference
that relaxes **only** the sizing criteria:

- *Relaxed in compact:* target size (2.5.8, 24×24 CSS px minimum) and the
  padding/row-height minimums that flow from it.
- *Never relaxed:* contrast (4.5:1 text, 3:1 non-text — color tokens are
  density-independent), keyboard operability, visible/unobscured focus,
  semantics/labels, 200% text resize, reflow, reduced-motion.

Because compact is opt-in and the default conforms, the AA claim holds:
"AA by default; compact trades the target-size minimum for density."

**Mechanics:** density = one spacing/size token-set swap (comfortable ↔
compact) at the shell level; every kind component consumes tokens, so density
is uniform by construction. **Enforcement is CI, not vigilance:** axe-core
over rendered kinds in both densities and both light/dark themes, a palette
contrast validator, and keyboard-path checks — a failing token breaks the
build. Chart-bearing widgets (later) inherit: never color-only encoding,
colorblind-safe series palette.

## 8. Build order

1. **Shell + native views** (this repo, platform): Vite app, token system +
   densities, nav, home grid, plugins/surfaces/scopes pages, CI a11y checks,
   `ACCESSIBILITY.md` (+ register it).
2. **Manifest `ui` block + `/ui-api` proxy** (platform): validation, SDK
   schema, fail-visible rendering, proxy with allowlist + health
   short-circuit.
3. **First registered contribution** (governance): `/ui-api` JSON routes over
   the existing shadow service layer; `stat` widget + `table` branches page +
   `thread`/`document` branch view.
4. **Roadmap** (pm repo, tracked there — `roadmap-board.md`): the pm roadmap
   moves off the `table`-with-indentation stopgap (`ui_api.py`'s current
   `_item_row(indent=True)`) onto `board` (§4.2a) — real nesting, gating
   badges, org grouping/facets; the action contract lands whenever pm's
   roadmap first needs a browser write.

## 9. Out of scope (v1)

- Writes from the browser (action *rendering*; the contract shape ships in v1
  schemas). *Superseded: `shadow-conversations.md` activates proxy POST + the
  thread composer, and issue #123 activates PAGE-LEVEL `actions[]` rendering
  (§5.1). Finer-grain row/thread-node actions and reorderable boards still land
  with the pm work.*
- Remote-module escape hatch (documented direction only).
- `search` page kind; websockets/SSE liveness (poll first); mobile-dedicated
  layouts (responsive reflow only); theming beyond light/dark.
- Auth beyond the tailnet edge (unchanged platform posture).
