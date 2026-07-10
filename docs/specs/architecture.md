# Snowline architecture

> **Status: draft.** The system-level map — what Snowline is, the platform/plugin
> model, the core principles, and how the pieces fit. Component specs
> (`governance-plugin.md`, `gateway.md`, scope namespace, …) sit alongside this
> and detail each part. This is the doc to read first.

## 1. What Snowline is

An **agent-native platform** that hosts capabilities as **MCP-server plugins**.
The flagship capability is **governance-memory** — a durable, cross-session
record of decisions, a shadow/speculation graph, and governing specs. The design
bet: keep the server **thin** — plugins are structured read/write surfaces over
their own stores, and **the LLM (an agent like Claude) is the integration
runtime**, the place where capabilities actually meet.

## 2. Platform vs plugins

- **The platform** (this repo): the **gateway** + **plugin registry** +
  **health/supervision** + the **scope namespace** + the **trust gate**. Small,
  but real — it owns the universal primitive (scopes) and composes everything.
- **Plugins**: independent modules, each exposing **MCP surface(s) + a UX** over
  its **own store**. Governance is the flagship; later: a private PM, a GitHub
  plugin, drift/triage carriers.
- **Dependency direction**: plugins depend on the platform (they register and use
  scopes); the platform depends on no plugin.

## 3. Core principles

1. **The LLM is the integration runtime.** MCP surfaces are structured CRUD; the
   server does **no cross-domain synthesis**. Every cross-capability action is the
   agent reading one plugin's surface and writing another's — so plugin↔plugin
   coupling is ~nil and the platform never needs to route between plugins.
2. **Scopes are the shared spine.** The platform owns the scope namespace —
   identity + the `parent_id` tree + the `isolated` flag. Every plugin references
   scopes by slug; **isolation and inheritance are properties of that tree**, so
   all plugins share one notion of "where am I and what's above me."
3. **Plugins are out-of-process, addressed by URL.** Local or cross-tailnet —
   the platform proxies over HTTP either way. New plugins **register without a
   platform restart** (hot-pluggable). Cross-machine cleanly enforces the
   public/private split: a private plugin runs on the owner's own box and
   registers over the tailnet, so its code + data never touch the public host.
4. **Surfaces are composed, isolation is structural.** The platform exposes
   **named MCP surfaces** (`main`, plus isolated ones like `shadow`); plugins map
   their own surfaces onto them; the gateway aggregates per surface. A tool only
   appears on a surface a plugin explicitly placed it on — so e.g. `record_decision`
   is *physically* absent from `shadow`. Same idea for UIs (a separate shadow UI).
   The surface SET is config (`SNOWLINE_SURFACES`), and per-surface plugin
   membership can be subset with `SNOWLINE_SURFACE_PLUGINS` (e.g. `main=*;core=governance`)
   so a surface can be composed with or without a given plugin — the split's
   "governance-only, no PM" `core` surface alongside the full `main` — without
   editing any plugin manifest (allowlist at the aggregation step; see `gateway.md`).
5. **Trust is a pluggable gate.** A configurable trusted-CIDR network gate
   (tailnet plus loopback, both deliberately owner-trusted network-position —
   decision 35546152) today, OAuth as a drop-in provider later — behind one
   seam. The tailnet/loopback path stays zero-config (no per-client secrets);
   public exposure authenticates at an edge front instead (Snowline#120)
   rather than widening this gate.

## 4. Components

| Component | Role | Status |
|---|---|---|
| Trust layer | pluggable access gate (CIDR now, OAuth later) | built |
| Plugin registry + manifest | what plugins exist + how they declare themselves | built |
| Gateway | aggregate plugin surfaces onto named platform surfaces; proxy MCP + UI | spec'd (`gateway.md`) |
| Health + supervision | poll plugin health; restart local; route around down/unreachable | planned |
| Scope namespace | the shared spine plugins reference | spec pending |
| Governance plugin | the flagship capability (decisions + shadow + specs) | spec'd (`governance-plugin.md`) |
| Plugin SDK / contract | published versioned dep plugins consume (typed client + signed-event verify) | carried from prior work |

## 5. Topology + how it's built

- **Public** `snowlinedev/Snowline` = the platform; **governance** is public; the
  owner's **PM plugin is private** (runs cross-tailnet). Built **in public**.
- The prior monolith is **frozen** — it keeps running as the owner's daily driver
  until migration, but receives no new work. The platform is built fresh and kept
  **schema-compatible** with the monolith so existing governance data (decisions,
  scopes) migrates into the running instance later.
- **Spec-first**: every component gets a handoff-grade spec of expected
  functionality before/as it's built — carve-outs included (the spec is the
  behavior contract; carving is just the route).

## 6. Status & order

Trust + registry shipped; governance + gateway specs written. Dependency-driven
build order: **scope namespace → governance plugin → gateway / health** (the
gateway needs a real plugin to compose; governance needs the scope tree).
