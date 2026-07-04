# Snowline

An agent-native **platform**: it hosts capabilities — governance/decision memory,
working memory, integrations — as **MCP-server plugins**, composes their
surfaces into one endpoint an agent (Claude) connects to, and owns the shared
**scope** namespace every plugin hangs off.

The design bet: keep the server-side **thin** — plugins are structured read/write
MCP surfaces over their own stores, and the **LLM is the integration runtime**
(the place where governance, memory, and the rest actually meet). The platform's job
is to compose plugin surfaces + UIs, supervise the plugin processes, gate access,
and hold the scope namespace.

> Built in the open, with Claude — the commit history and the decision log are
> the point, not an afterthought.

## Status

Early. Building the platform foundation first:

- [x] **Trust layer** — a pluggable access gate. v1 is a configurable
  trusted-CIDR network gate (the tailnet); the seam is designed so OAuth slots
  in later as another provider without touching downstream code.
- [ ] Gateway + plugin registry (compose plugin MCP surfaces + UIs under one
  endpoint; supervise + health-check plugin processes; plugins addressed by URL,
  so local or cross-tailnet).
- [ ] Scope namespace (the shared spine plugins reference).
- [ ] First plugin: **governance** (decisions + shadow/speculation + specs).
- [ ] **memory** plugin (cross-folder agent session memory — remember / recall /
  digest, over its own store).

## License

Apache-2.0.
