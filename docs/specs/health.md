# Spec — Plugin health checks & route-around

Status: draft · Issue: #3 · Builds on: plugin registry + manifest, gateway
(`docs/specs/gateway.md` §4).

## Why

Plugins are **out-of-process, URL-addressed** modules (local or cross-tailnet).
Any of them can crash (local) or become unreachable (network) independently of
the platform. The platform must notice and **route the gateway around** a plugin
that is down so one dead plugin neither hangs a surface nor blanks the others —
and recover it automatically when it comes back. Crashed-local and
unreachable-remote are treated **identically**: route-around, surface the state,
keep retrying.

This is the missing half of the gateway's health-aware composition: the gateway
already *consults* `RegisteredPlugin.status` (skips `DOWN`, treats `UNKNOWN` as
routable). This component is what *sets* that status.

## Expected functionality

### Health signal

A plugin is **healthy** when an HTTP `GET` of its `base_url + health_path`
(manifest fields; `health_path` defaults to `/health`) returns a 2xx within the
poll timeout. Anything else — non-2xx, connection refused, DNS failure, TLS
error, timeout — is **unhealthy**. There is no body contract; 2xx *is* the
contract (matches the platform's own `/health` returning `{"status": "ok"}`).

### Status mapping

The poll maps each plugin to a `PluginStatus`:

| Observation                               | Status |
|-------------------------------------------|--------|
| 2xx within timeout                        | `UP`   |
| non-2xx, or any transport error / timeout | `DOWN` |

`UNKNOWN` is only the *pre-first-poll* state (set at registration). Once a plugin
has been polled it is always `UP` or `DOWN`. Recovery is automatic: a `DOWN`
plugin that starts returning 2xx flips back to `UP` on the next poll, with no
operator action.

### The poller

A single background task, started in the platform app lifespan and cancelled on
shutdown:

- Every `interval` seconds (`SNOWLINE_HEALTH_POLL_INTERVAL`, default 15s) it
  polls **all** currently-registered plugins.
- Each plugin is checked with a per-request `timeout`
  (`SNOWLINE_HEALTH_POLL_TIMEOUT`, default 5s) so one slow plugin cannot stall
  the round.
- Plugins in a round are polled **concurrently** — round wall-clock is ~one
  timeout, not N × timeout.
- Each result is written back via `registry.set_status(name, status)`, which is
  thread-safe and a **no-op if the plugin was unregistered mid-poll** (the poller
  never resurrects a removed entry).
- A failure checking one plugin never aborts the round or the loop — it is that
  plugin's `DOWN`, nothing more. The loop itself catches and logs any unexpected
  error and continues to the next tick (the poller must outlive transient
  faults).

The registry is in-memory and shared with the gateway, so a status change is
visible to the very next gateway request (the gateway re-discovers upstreams per
request). No restart, no cache to invalidate.

### Interaction with the gateway

Unchanged gateway code: `discover_upstreams` skips `DOWN`, so the moment the
poller marks a plugin `DOWN` it disappears from every named surface's tool list
and becomes unroutable (calls return a clear `GatewayError`, never a hang). When
the poller flips it back to `UP`, it reappears. `UNKNOWN` remains routable so a
freshly-registered plugin works in the window before its first poll.

## Out of scope (later add-ons)

- **Local supervision** — restarting a crashed *local* plugin process. Remote
  plugins are gateway + health only; the platform cannot restart a process on
  another host. A future supervisor would act on `DOWN` for plugins the platform
  launched.
- **Flap damping / backoff** — a steady interval is enough for v1; exponential
  backoff for persistently-down plugins and hysteresis to avoid flapping are
  refinements.
- **Health history / alerting** — only current status is tracked. Surfacing a
  status timeline or notifying on transitions is a dashboard concern.

## Configuration

| Env var                         | Default | Meaning                          |
|---------------------------------|---------|----------------------------------|
| `SNOWLINE_HEALTH_POLL_INTERVAL` | `15`    | seconds between poll rounds      |
| `SNOWLINE_HEALTH_POLL_TIMEOUT`  | `5`     | per-plugin request timeout (s)   |

The poller runs only when the app is built with health polling enabled
(`create_app(poll_health=True)`, as the production singleton is). The
test-friendly factory defaults it **off** so unit tests don't spawn network
traffic or race on status.
