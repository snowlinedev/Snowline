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

`search` (query box + result list, for shadow corpus search) is anticipated
but **deferred** until the read views prove out.

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
    { "name": "scope", "label": "Scope", "kind": "text", "required": true },
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
| `kind` | no | rendering hint: `text` (single line, default) or `multiline` (textarea). A FREE string — an unknown value falls back to a text control at render, it does not reject the manifest |
| `required` | no | the shell blocks submit until this is non-blank (default `false`) |

**Shell rendering.** A `label` button sits above the page's kind content;
clicking it opens a form of the declared fields (text→input, multiline→
textarea). Submit POSTs `{ <name>: <value>, … }` as JSON through the proxy to
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
4. **Later** (pm repo, tracked there): roadmap pages via `table`/`board` +
   the action contract; shell grows action rendering then.

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
