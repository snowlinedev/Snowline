# Gateway

> **Status: draft.** How the platform composes registered plugins into the
> surface(s) clients connect to. The functional contract for issue #2.

## 1. Purpose

The gateway is what makes Snowline a *platform*: it turns N independently-running
plugins into the **unified surface(s)** a client connects to — the daily-driver
agent over MCP, and the browser over the UI. It reads the **plugin registry** and
composes; it never imports plugin code (plugins are addressed by URL).

## 2. MCP surface aggregation (the core job)

- The platform exposes **named MCP surfaces** — `main` (the composed surface the
  agent connects to) and isolated ones like `shadow`; extensible.
- Each plugin's manifest **maps its own surfaces** onto platform surfaces
  (governance: `/mcp → main`, `/shadow/mcp → shadow`). Most plugins map one (→
  `main`).
- For each platform surface, the gateway **aggregates** the tools of every
  plugin-surface mapped to it into the single surface the client sees:
  - **List**: merge the upstream plugins' tool lists.
  - **Call**: route each `tools/call` to the owning plugin and stream the result
    back.
  - **Transport**: proxy MCP **streamable-HTTP** to the plugin's `base_url` +
    path, preserving session + streaming semantics. *(This is the meatiest
    implementation risk — session affinity across the proxy.)*
- **Isolation is plugin-side and structural**: the gateway composes *whole*
  surfaces and never reasons about individual tools, so a tool appears on a
  surface only because a plugin mapped it there (`record_decision` never lands on
  `shadow`).

## 2a. Per-surface plugin allowlists (config)

The surface SET is configuration (`SNOWLINE_SURFACES`); surface MEMBERSHIP is
manifest-driven — every plugin that maps a path onto a named surface lands there.
`SNOWLINE_SURFACE_PLUGINS` lets the platform **subset** that membership per
surface, so a surface can be composed **with or without** a given plugin without
touching any plugin manifest. This is the product split's daily need: the platform and
governance are public while the owner's private plugin is not — a `core`
surface must be able to express "governance-only, without the private plugin"
while `main` stays the full composed daily driver. It's
an allowlist at the aggregation step (decision `70b415fd`: named surfaces, gateway
aggregates), not a new model.

- **Format:** `SNOWLINE_SURFACE_PLUGINS="main=*;core=governance"` — `;`-separated
  surface entries, each `<surface>=<allowlist>`; the allowlist is `*` (every
  plugin) or a `,`-separated list of plugin names. Whitespace is tolerated.
- **Default = allow-all.** A surface with no entry (and the empty/unset env)
  aggregates every plugin — fully backward compatible.
- **Fail loud — the env is fully validated at boot.** This is an EXCLUSION
  boundary, so a config mistake must kill startup, never silently widen a
  surface (e.g. leave a private plugin reachable on a governance-only surface). `create_app`
  (via `build_surface_mounts` → `config.surface_plugins()` +
  `config.validate_surface_plugins()`) raises `ConfigError` for ALL of:
  - malformed shape — no `=`, empty name/allowlist, duplicate surface, stray
    comma, `*` mixed with names;
  - a bad SURFACE name — left-hand names must be lowercase url-safe slugs
    (`[a-z0-9][a-z0-9-]*`, the shape `/X/mcp` routes need); `*` is only legal
    on the RIGHT side;
  - a bad PLUGIN token — right-hand names must match the manifest name rule
    (`manifest.PLUGIN_NAME_RE`); `core=Governance` could never match a
    registered plugin and would silently empty the surface, so it's rejected;
  - an allowlist naming a surface NOT in the mounted set (see interplay below).
- **Parsed once, at mount time.** `build_surface_mounts` parses + validates the
  env once and hands each surface its FROZEN allowlist; `discover_upstreams`
  never re-reads the env. Fail-at-boot is structural: there is no per-request
  re-parse and no mid-run `ConfigError` path. A config change is a restart,
  same as the surface set itself.
- **Aggregation-only.** The filter applies in `gateway.discover_upstreams` (by
  plugin name), so a filtered plugin is absent from BOTH `list_tools` and
  `call_tool` routing on that surface. Registration, health, and the registry
  views are unchanged — this filters what a surface *composes*, not what is
  *registered*.
- **Projection: an allowlisted surface composes ROOT_SURFACE mappings as the
  fallback (issue #38).** An allowlist is an operator statement of composition
  ("`core` = governance only"), but no real plugin's manifest maps
  anything onto an operator-invented surface name (governance maps only
  `/mcp → main` + `/shadow/mcp → shadow`) — a pure filter over manifest
  mappings therefore mounted an EMPTY `/core/mcp`, found live minutes after
  the filter shipped. So a surface WITH an explicit allowlist composes, per
  allowlisted plugin:
  - the plugin's **native** mapping for that surface, when its manifest
    declares one;
  - **else** its `ROOT_SURFACE` (`main`) mapping — the plugin's daily-driver
    tools, projected onto the constrained surface with no plugin-side manifest
    change;
  - **else** (neither mapping) nothing — the plugin simply doesn't contribute.

  A native mapping wins *outright*: the `main` mapping is not also projected,
  so projection never creates a duplicate `(plugin, surface)` path (the
  issue-#22 duplicate-path guard stays reserved for genuine manifest errors).
  A surface WITHOUT an allowlist never projects — membership stays purely
  manifest-driven, byte-for-byte the pre-allowlist behavior — so `main` tools
  can never leak onto an isolation surface like `shadow`. Projection is
  strictly a property of the explicit allowlist.
- **Interplay with `SNOWLINE_SURFACES` — list a constrained surface in BOTH
  envs.** `SNOWLINE_SURFACES` alone decides the mounted set; there is NO
  auto-include of allowlist-named surfaces. An allowlist naming an unmounted
  surface raises `ConfigError` at boot instead. Rationale: auto-include turned a
  left-hand typo (`coer=governance` while `SNOWLINE_SURFACES` has `core`) into a
  silently-mounted dead `/coer/mcp` while the real `core` stayed ALLOW-ALL —
  exactly the silent widening this feature exists to prevent. The cost is one
  extra line of config:

  ```sh
  SNOWLINE_SURFACES="main,shadow,core"
  SNOWLINE_SURFACE_PLUGINS="core=governance"
  ```

  `ROOT_SURFACE` (`main`) stays the one always-present magic name.

Result: `http://<host>:8850/core/mcp` serves the governance-only composition — governance's
projected `main` tools — over the tailnet while `/mcp` stays the full composed
daily driver.

## 3. UI composition

Each plugin's manifest declares its UI; the gateway serves/proxies it under the
plugin's route. The **shadow UI is a separately-mounted module** — UX isolation
mirrors the MCP isolation (a human can't act on live decisions from the shadow
view).

## 4. Health-aware routing

The gateway consults registry **status** (set by the health checker): it does not
route to a plugin that is `down`/unreachable — it route-arounds and surfaces a
clear error rather than hanging on a dead upstream. "Crashed" (local) and
"unreachable" (network) are treated the same.

## 5. Addressing

Plugins are addressed by `base_url`, so **local or cross-tailnet** — the gateway
proxies over HTTP regardless of where a plugin runs. A cross-tailnet plugin is
just a different URL.

## 6. Acceptance criteria

- A registered plugin's tools appear on its mapped platform surface; `tools/call`
  routes to that plugin; streaming responses work end to end.
- Two plugins mapped to `main` → their tools are merged into one `/mcp` the client
  sees.
- A real-write tool a plugin maps only to `main` is provably **absent** from
  `shadow`.
- Unknown/unregistered route → 404; a `down` plugin → route-around, not a hang.

## 7. Open / deferred

- **Tool-name collision policy** when two plugins on the same surface expose the
  same tool name — namespace by plugin, or reject at registration? (Decide before
  the second plugin shares `main`.)
- **Cross-plugin grounding** (one plugin's read tools placed onto another's
  surface — e.g. another plugin's reads on `shadow`) — deferred; additive per-tool-group
  placement if ever needed.
- **In-process fast path**: not pursued — out-of-process + URL addressing is what
  enables hot-plug and cross-machine; the gateway stays a proxy.
