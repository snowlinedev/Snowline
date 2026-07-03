# Deploy continuity — toward zero-downtime deploys

> **Status: draft.** What it takes for deploys and crash-restarts to be
> invisible to live agent sessions, layered so each layer stands alone.
> Layer 1 is ALREADY SHIPPED and is recorded here as standing posture;
> layer 2 is the buildable v1 scope; layers 3–4 are deferred capabilities
> with stated revisit triggers (issues #58, #59). Design session 2026-07-03
> (Sean); builds on the registration heartbeat (#39/#49).

## 1. The problem, precisely

The heartbeat made deploys *self-healing* but not *invisible*. What a live
session still sees:

1. **Plugin kickstart** — gateway→plugin connects fail for the sub-second-to-
   few-seconds the plugin process is down; those tool calls hard-fail even
   though the plugin is back moments later. (Health route-around doesn't
   cover this: brief restarts never get marked DOWN.)
2. **Platform kickstart** — the listening socket itself drops for ~a second;
   the client's next call is refused. Client-side behavior (Claude Code does
   not transparently retry; tools vanish — the #39 field reports) makes even
   a one-second blip session-fatal. Server-side we can only shrink the blip
   to zero (layer 3); we do not own the client.
3. **Registry-empty window** — a freshly-booted platform serves hollow
   surfaces for ≤1 heartbeat interval (mitigated by loud warnings, #49).

## 2. Layer 1 — stateless MCP surfaces (SHIPPED; standing posture)

Already true everywhere, and this spec pins it as a commitment:

- The gateway's composed surfaces run `StreamableHTTPSessionManager(...,
  stateless=True)` (`gateway_app.py`).
- Governance (main + shadow) and memory build `FastMCP(...,
  stateless_http=True)`; pm mirrors the same construction.
- The gateway opens **per-request** upstream sessions (`gateway.py`) — no
  gateway↔plugin session outlives a call.

Consequence: no process holds session state a restart can orphan. A restart
costs only the requests in flight during the blip — never the session.

**Constraint this imposes:** surfaces stay request/response only (tools; no
server-initiated notifications, no sampling, no session-scoped server state).
That is the platform's existing "LLM is the integration runtime" posture —
but any future feature wanting server-push must revisit THIS spec first,
because it would re-couple sessions to processes and forfeit the layer.

*Do layers 1–2 help governance/memory?* Layer 1 already covers them (their
statelessness is why a plugin restart costs only in-flight calls). Layer 2 is
**mostly for them**: plugin kickstarts are the common deploy, and the gateway
is the single place that can absorb them.

## 3. Layer 2 — gateway connect-phase retry (the v1 build, issue #57)

When the gateway's per-request upstream connect fails, retry briefly before
failing the call: the plugin is usually mid-kickstart and back within a
second or two.

**The safety line — retry only what provably never executed.** A tool call
is not idempotent in general. The gateway may retry a failure ONLY when it
occurred before the upstream received the call:

- **Retryable:** connection refused / unreachable / reset during the
  connect+initialize phase of the per-request upstream session — nothing was
  delivered.
- **Never retried:** any failure after `call_tool` was written to the wire
  (timeouts mid-call, malformed responses, tool-level errors) — the plugin
  may have executed the write.
- `list_tools` is read-only and may retry in both phases.

**Policy (implementation-time tunable):** 2 retries, ~250ms then ~750ms
backoff, bounded by the request's overall deadline. Health interplay
unchanged: DOWN plugins are still routed around *before* any connect is
attempted; retry covers the UP/UNKNOWN-but-restarting window that health
polling is too slow to see. Retries log at DEBUG (steady-state silence), with
a WARNING only when retries were exhausted.

**Effect once shipped:** plugin deploys (governance, memory, pm) become
invisible — a tool call issued mid-kickstart pauses ~a second instead of
failing. Platform deploys remain visible (the socket itself; layer 3).

## 4. Layer 3 — platform socket continuity (DEFERRED, issue #58)

Blue/green for the platform process so even its restarts drop zero
connections. Sketch (decide at implementation): a tiny reverse proxy (Caddy)
owning `100.81.176.75:8850` with uvicorn instances on localhost ports and a
drain-and-flip deploy script — or SO_REUSEPORT overlap (fewer moving parts;
macOS semantics need a prototype). Requires **registry warm-start** (seed
green from blue's `GET /plugins`, or shorten heartbeats to ~3s, or the #39
option-2 persistence — which this layer would finally justify).

**Revisit trigger:** a platform deploy interrupts real agent work despite
layers 1–2; OR a second daily-driver user appears; OR the platform moves to a
Linux host (where the proxy pattern is the native idiom anyway).

## 5. Layer 4 — expand/contract migration discipline (DEFERRED, issue #59)

Boot-migrate means a new process migrates the DB while the old one still
serves. Zero-downtime overlap therefore requires migrations be
backward-compatible one deploy back: additive first (expand), destructive
only after the code that needed the old shape is gone (contract).

**Revisit trigger:** layer 3 lands (overlap becomes real); OR the first
migration that would break the previous release running against the new
schema.

## 6. Packaging posture — containers (recorded decision context)

Containerizing does not advance any layer: 1–2 are app-level, 3 still needs
a traffic-flipper, 4 is convention. On the current macOS host it is mildly
hostile (VM NAT between the platform and the tailnet bind the trust gate
keys on; slower dogfood loop; unix-socket Postgres would need moving).
Containers become right when either (a) the platform moves to Linux — where
layer 3 arrives nearly free via standard orchestration — or (b) Snowline
ships to others and a compose file is the install story. Until then: stay
12-factor-clean (env-driven config, no host paths, DB by URL — all true
today) so the option stays cheap. Note the fleet stays mixed forever:
walkthrough-mcp requires macOS `simctl` and will always be a native daemon;
URL-addressed plugins over the tailnet make that a non-event.

## 7. Out of scope

- Client-side retry behavior (Claude Code's MCP client is not ours).
- Multi-host platform HA / failover (single-host blue/green only).
- Auth changes; the tailnet remains the trust boundary throughout.
