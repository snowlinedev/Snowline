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
  surface — e.g. PM reads on `shadow`) — deferred; additive per-tool-group
  placement if ever needed.
- **In-process fast path**: not pursued — out-of-process + URL addressing is what
  enables hot-plug and cross-machine; the gateway stays a proxy.
