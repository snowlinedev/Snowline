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
- **Platform-native tools = the platform registering ITSELF as an upstream**
  (decision `0503fff0`, 2026-07-23). The platform's own tools (scope + milestone
  registry verbs — `scope-namespace.md` §4, `milestones.md` §5) are NOT an
  in-aggregator special case. The platform mounts an MCP streamable-HTTP tool app
  on its own HTTP app at `/platform/mcp`, and seeds a `platform` **registry
  entry** at its own loopback `base_url` mapping `{"/platform/mcp": "main"}`. The
  gateway then composes it onto `main` through the *ordinary* `discover_upstreams`
  path — the aggregator does not know this upstream is the platform — and the
  tools surface namespaced `platform__<tool>` by the same `<plugin>__<tool>`
  convention. This keeps the invariant above intact (compose whole surfaces,
  never reason about individual tools) and dogfoods the composition path, with
  exact precedent in the platform's replication self-participation
  (`replication-continuity.md` §8, "the SDK's own publisher"). The self-entry is
  a plain manifest: it appears in `GET /plugins`, is health-checked against its
  own loopback `/health` like any plugin, is ABSENT from isolation surfaces it
  does not map (`shadow`), and PROJECTS onto an explicitly-allowlisted surface via
  its `ROOT_SURFACE` mapping exactly like any plugin (§2a).

## 2a. Per-surface plugin allowlists (config)

The surface SET is configuration (`SNOWLINE_SURFACES`); surface MEMBERSHIP is
manifest-driven — every plugin that maps a path onto a named surface lands there.
`SNOWLINE_SURFACE_PLUGINS` lets the platform **subset** that membership per
surface, so a surface can be composed **with or without** a given plugin without
touching any plugin manifest. This is the product split's daily need: the public
Snowline drives GitHub directly, while the private PM plugin is the owner's
bespoke roadmap engine — a `core` surface must be able to express
"governance-only, no PM" while `main` stays the full composed daily driver. It's
an allowlist at the aggregation step (decision `70b415fd`: named surfaces, gateway
aggregates), not a new model.

- **Format:** `SNOWLINE_SURFACE_PLUGINS="main=*;core=governance"` — `;`-separated
  surface entries, each `<surface>=<allowlist>`; the allowlist is `*` (every
  plugin) or a `,`-separated list of plugin names. Whitespace is tolerated.
- **Default = allow-all.** A surface with no entry (and the empty/unset env)
  aggregates every plugin — fully backward compatible.
- **Fail loud — the env is fully validated at boot.** This is an EXCLUSION
  boundary, so a config mistake must kill startup, never silently widen a
  surface (e.g. leave PM reachable on a governance-only surface). `create_app`
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
  ("`core` = governance, without PM"), but no real plugin's manifest maps
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

Result: `http://<host>:8850/core/mcp` serves governance-without-PM — governance's
projected `main` tools — over the tailnet while `/mcp` stays the full composed
daily driver.

## 3. UI composition

Each plugin's manifest declares its UI; the gateway serves/proxies it under the
plugin's route. The **shadow UI is a separately-mounted module** — UX isolation
mirrors the MCP isolation (a human can't act on live decisions from the shadow
view).

## 3a. Plugin HTTP surfaces (proxying beyond MCP + ui)

A plugin may serve contracts that are neither MCP tools nor shell views — an
ordinary HTTP API another program consumes. The motivating one: the PM plugin
serves musher's **work-item provider contract** (`GET /provider/work-items`,
`GET /provider/work-items/{id}`, `POST /provider/work-items/{id}/dispatched`).
Without gateway support, musher's `MUSHER_PROVIDER_URL` must point at the pm
process's own port — so the address breaks whenever pm moves port or host, and
the contract sits outside the platform's trust gate. The `http` manifest block
closes that gap: **the gateway proxies declared plain-HTTP prefixes at its own
root**, so `MUSHER_PROVIDER_URL` is the gateway base URL and stays true across
plugin redeploys.

**Vocabulary.** A manifest may declare `http: [{prefix, methods, description}]`:

```json
{"name": "pm", "base_url": "http://127.0.0.1:8802",
 "http": [{"prefix": "/provider", "methods": ["GET", "POST"],
           "description": "musher work-item provider contract"}]}
```

- `prefix` — a **root-level** path prefix of one or more LITERAL segments. No
  `{param}` segments (a prefix is matched, never templated), no `..`/`.`, no
  trailing slash, no empty segment.
- `methods` — the uppercase methods the gateway forwards under the prefix; one
  of `GET`/`POST`/`PUT`/`PATCH`/`DELETE`. Defaults to `["GET"]`. Anything else
  is a 422 at registration: the gateway would never forward it, so it is an
  authoring error, not a fail-visible degradation.
- Two prefixes in ONE manifest may not collide — equal, or one containing the
  other (`/provider` + `/provider/x`). Longest-prefix *would* resolve the
  nesting, but the two surfaces' `methods` then silently disagree about the
  same request.

**Root-level, verbatim.** Unlike `/ui-api/<plugin>/<path>` (§5), which
namespaces by plugin and fixes the upstream prefix, the public path here **is**
the plugin path: `GET /provider/work-items` on the gateway becomes `GET
<base_url>/provider/work-items` upstream — the raw, still-percent-encoded path
(so `%2F`/`%23`/`%25` in an identifier survive), a trailing slash if one was
sent, and the query string as raw bytes (every value of a repeated key). A
`base_url` with its own path keeps it; a `base_url` carrying a query or
fragment is refused at registration (it would swallow the appended path).
That is the point — an unmodified consumer talks to the gateway exactly as it
would talk to the plugin. Matching is **segment-aligned**: `/providerx` does
not match `/provider`. Nothing is normalized: a path with a `.`/`..` segment or
an empty interior segment is simply not the proxy's (404) — the `/ui-api`
lesson that a normalized path can match one prefix and land on another.

**Reserved prefixes.** Because prefixes are root-level, they share a namespace
with the platform's own routes. A prefix whose first segment is one the
platform serves — the MCP surface mounts (`/mcp`, `/<surface>/mcp`,
`/platform/mcp`), `/ui`, `/ui-api`, `/plugins`, `/scopes`, `/milestones`,
`/surfaces`, `/replication`, `/replication-admin`, `/health`, `/whoami`,
`/events`, and FastAPI's own docs routes — is refused. Two lines of defense:
the STATIC set `manifest.RESERVED_HTTP_PREFIXES` refuses at validation (422)
and is kept honest by a test that walks the built app's routes; and the
registry holds the LIVE set — every top-level segment the built app actually
routes, config-named surfaces included (`SNOWLINE_SURFACES=…,ops` mounts
`/ops/mcp`) — handed over by `create_app` after the last mount, and refuses
at upsert (409, holder `platform`). A surface that exists only in config can
therefore never be half-shadowed by a plugin prefix.

**Registration-time collision refusal — loud.** A prefix that collides with one
held by a **different** registered plugin (equal, or either containing the
other) is refused: `POST /plugins` answers **409** naming the prefix and the
holding plugin, and the platform logs a WARNING. The registry owns this
invariant, not the route — it is the only place the whole plugin set is visible
under one lock. The refused plugin's registration heartbeat (issue #39) re-POSTs
every beat, so the WARNING **repeating** is the operator-visible signal that two
plugins contend for one prefix — the same loudness posture as the
manifest-REPLACED warning. The SDK heartbeat treats the 409 as what it is — a
REFUSAL, logged at WARNING every beat, never "already registered" (the
register verb is an upsert; 409 has no other meaning) — so a refused plugin
never reports itself confirmed. The same plugin re-registering is always fine:
a heartbeat is `unchanged`, and a redeploy that re-shapes its OWN prefixes is
`updated`.

**Per-request proxying.** The proxy is wired as the router's **fallback**, not
as a catch-all route: it is reached only after every platform route failed to
match *and* after Starlette's redirect-slashes pass (so `GET /mcp` still 307s to
its mount — a literal `/{path:path}` route would match `/mcp` and silently break
every MCP client). A path no plugin claims falls through to the app's ordinary
404.

- **Headers are a denylist** (the reverse-proxy norm), in both directions.
  Stripped on the way up: hop-by-hop headers, `host` (the upstream sees its
  own; the gateway's rides in `X-Forwarded-Host`), request `content-length`
  (recomputed from the buffered body), the caller's platform credentials and
  cookies (`authorization`, `cookie`), and any forwarding headers a caller
  invented — the gateway sets its own `X-Forwarded-For` (the direct peer),
  `X-Forwarded-Host`, `X-Forwarded-Proto` and `X-Snowline-Gateway: 1`. A
  caller that sends no `accept-encoding` gets `identity` upstream (httpx
  would otherwise volunteer compression the caller never asked for).
  Everything else passes — a whitelist would silently disable the
  mechanisms a contract consumer relies on (`If-None-Match`/`If-Match`,
  `Idempotency-Key`, `Retry-After`, `Vary`). Stripped on the way back:
  hop-by-hop, `server`/`date` (uvicorn's), and `set-cookie` (a plugin session
  must never be set on the gateway origin).
- **Redirects stay behind the gateway**: an absolute `Location` /
  `Content-Location` at the plugin's own `base_url` (Starlette's
  redirect-slashes and `url_for` build those from the host the upstream saw)
  is rewritten to the gateway origin; relative and external values pass.
- **The response is streamed**, not buffered — relayed as raw bytes as it
  arrives, so `content-encoding`/`content-length` stay truthful and a
  long-poll or event stream neither pins gateway memory nor delays first
  byte to EOF. The per-read upstream timeout is 60 s (a contract write that
  takes longer than a widget read must not come back "unreachable" after it
  applied); connect stays `/ui-api`'s 10 s.
- The request body is buffered with the same 64 KiB cap `/ui-api` POSTs use
  (413 past it, enforced on the actual bytes, never on a Content-Length that
  can lie).
- A method not in the surface's `methods` → **405** with `Allow`, without a
  round-trip. A path that cannot be expressed as an upstream URL → **400**.
- An `httpx.HTTPError` → **502**, with the same log shape `/ui-api` uses.
- **No retry.** A connect failure is a straight 502, exactly as `/ui-api` does.
  Connect-phase retry (`deploy-continuity.md` §3) is the MCP gateway's concern,
  where a redeploy mid-tool-call is invisible to the agent; an HTTP consumer
  sees the status code and owns its own retry policy.

**Health route-around** (§4): a plugin whose registry status is `down`
short-circuits to **503** without a network round-trip; `unknown`/`up` proceed —
the same routability rule `/ui-api` and `discover_upstreams` apply.

**Trust posture.** Proxied paths are **not** exempt from the trust middleware
(only `/health` is): an untrusted peer gets the tailnet CIDR gate's 403 before
the router runs. The gateway does not make a plugin public — the plugin's own
bind stays loopback/tailnet, and the gateway is the single gated front door.

**Introspection.** Each entry's `http` block rides `GET /plugins`; `GET
/plugins/http-routes` is the flat operator view — `{prefix, methods, plugin,
description}` sorted by prefix — answering "what does the gateway root serve,
and who answers it".

**Deploy order.** `PluginManifest` TOLERATES unknown top-level keys (only its
nested blocks are `extra="forbid"`), so a manifest carrying `http` registers
fine against a platform that predates this revision — and is **silently
unproxied**: registration succeeds, `GET /plugins/http-routes` is empty, and a
consumer pointed at the gateway root gets the ordinary 404. That is the N−1
posture (an older platform must not refuse a newer plugin), but it means the
gate is operational, not fail-loud: **the platform deploys first**, and an
operator wiring a consumer to the gateway checks `/plugins/http-routes` lists
the prefix. The plugin's SDK pin must also be at or past this revision, or its
heartbeat reads a collision 409 as success. Within a packaged release train
the pieces ship together, and the ordering is the train's.

## 3b. Replication via the gateway (`/via/<plugin>/…`)

A **peer instance's** replication traffic — the §5 admin handshake, event
ingest deliveries and the §7 seed snapshot (replication-continuity.md) —
reaches this instance's plugins **through this gateway**, never at a plugin's
own port (governance decision 0b8390f7, item 6754a127). The gateway mounts
`/via` (`http_proxy.ReplicationViaProxy`): `/via/<plugin>/<path>` forwards
`<path>` VERBATIM — raw path, raw query, the §3a header denylist, a streamed
relay — to the named plugin's loopback `base_url`, so the peer's HMAC over the
exact body bytes survives the hop.

**Only two surfaces are served**, both read off the plugin's manifest
`replication` block: its declared `ingest_path`, and anything under the SDK
admin prefix `/replication-admin`. Everything else — the plugin's MCP mount,
`/health`, `/ui-api`, an `http` contract, a path under or beside the ingest
path — is the app's plain 404, as are an unknown plugin name, a plugin with no
`replication` block, and a malformed path (the §3a dot-segment rule). A
plugin whose status is `down` short-circuits to 503 (§4). A real `Mount`,
not a router fallback: `via` is in `RESERVED_HTTP_PREFIXES`, so no plugin
can claim it and nothing can shadow it.

**Sizing differs from §3a**: a delivery batch may be large (`VIA_BODY_LIMIT`,
8 MiB) and the snapshot route runs `pg_dump` before its first byte
(`VIA_READ_TIMEOUT`, one hour — the seed client's own bound).

**Trust.** The mount rides `TrustMiddleware` like every gateway path, so only
a trusted (tailnet/loopback) peer reaches it; the plugin then sees the
platform's own client as its peer — loopback — which the SDK's
`_require_trusted` already admits. No forwarded-header trust exists anywhere
on this path (the §3a `X-Forwarded-For` is informational). Net effect: the
only tailnet-reachable port per instance is the platform's; plugins keep
binding loopback and change nothing. Pairing addresses a peer's plugins as
`<peer platform_url>/via/<name>` by default (replication-continuity.md §4.1);
`advertised_base_url` remains the explicit override.

## 4. Health-aware routing

The gateway consults registry **status** (set by the health checker): it does not
route to a plugin that is `down`/unreachable — it route-arounds and surfaces a
clear error rather than hanging on a dead upstream. "Crashed" (local) and
"unreachable" (network) are treated the same.

## 5. Addressing

Plugins are addressed by `base_url`, so **local or cross-tailnet** — the gateway
proxies over HTTP regardless of where a plugin runs. A cross-tailnet plugin is
just a different URL. The reverse direction — a PEER instance reaching one of
THIS instance's loopback plugins — goes through `/via/<plugin>/…` (§3b).

## 6. Acceptance criteria

- A registered plugin's tools appear on its mapped platform surface; `tools/call`
  routes to that plugin; streaming responses work end to end.
- Two plugins mapped to `main` → their tools are merged into one `/mcp` the client
  sees.
- A real-write tool a plugin maps only to `main` is provably **absent** from
  `shadow`.
- Unknown/unregistered route → 404; a `down` plugin → route-around, not a hang.
- A plugin's declared `http` prefix is served at the gateway root and reaches the
  plugin unmodified; a second plugin claiming a colliding prefix is refused with
  a 409; a platform route is never shadowed by a declared prefix.

## 7. Open / deferred

- **Tool-name collision policy** when two plugins on the same surface expose the
  same tool name — namespace by plugin, or reject at registration? (Decide before
  the second plugin shares `main`.)
- **Cross-plugin grounding** (one plugin's read tools placed onto another's
  surface — e.g. PM reads on `shadow`) — deferred; additive per-tool-group
  placement if ever needed.
- **In-process fast path**: not pursued — out-of-process + URL addressing is what
  enables hot-plug and cross-machine; the gateway stays a proxy.
