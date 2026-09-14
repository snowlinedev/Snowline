# Hub-side Tailscale Serve — a stable tailnet URL for the desktop host

> Operational runbook for the "hosted interface" item (issue 39c092c9),
> layered on top of `docs/specs/macos-distribution.md` §5's loopback-only
> posture. The OPERATOR runs this on the hub — this repo ships only
> `ops/hub/tailscale-serve.sh` and this doc; nothing here runs automatically,
> and no CI job or installer invokes it.

> **Prerequisites discovered on the real hub (2026-09-01):**
> 1. **HTTPS certificates must be enabled for the tailnet** (admin console →
>    DNS → HTTPS Certificates). Without it, `tailscale serve --bg` HANGS
>    silently — no error, no config written. Enable the toggle first.
> 2. The macOS **GUI app** doesn't put `tailscale` on PATH; the script now
>    resolves the app-bundle CLI itself (override with `TAILSCALE_BIN`).
>    Invoking that binary via a symlink aborts on a bundle-identifier check —
>    call it by real path.

## 1. What it does

The hub (the always-on Mac mini, macOS distribution spec §1: "the primary
stays source-run") runs the platform bound to its own tailnet address, on
its real gateway port, `:8850`. `ops/hub/tailscale-serve.sh` configures
`tailscale serve` to front that one port with a stable **HTTPS** URL on the
tailnet.

One mapping is enough because the dashboard is not a separate service: the
platform serves the composed gateway API, the dashboard bundle (`/ui`), and
the dashboard's data proxy (`/ui-api`) all from the **same ASGI app on the
same port** (`src/snowline_platform/app.py`). So:

```
https://<hub>.<tailnet>.ts.net/         -> gateway (composed MCP + REST surface)
https://<hub>.<tailnet>.ts.net/ui       -> dashboard
https://<hub>.<tailnet>.ts.net/ui-api/… -> dashboard's data proxy
https://<hub>.<tailnet>.ts.net/health   -> health check
```

is ONE `tailscale serve --https=443 <backend>` rule, not one per surface.

This is the hub's ONLY tailnet exposure. Its plugins bind loopback and are
reached by a peer (the spoke's seed, pairing and deliveries) through the
gateway's `/via/<plugin>/…` proxy on this same port (replication-continuity
§4.1, decision 0b8390f7) — no per-service port mirror exists on the hub, and
none is needed. The roaming spoke's `ops/roam/tailscale-serve.sh` likewise
mirrors only the spoke's platform port.

## 2. The URL shape

`tailscale serve --bg --https=443 <backend>` publishes the node's own
MagicDNS name over HTTPS with a Tailscale-issued cert (no manual TLS setup):

```
https://<hub-hostname>.<your-tailnet>.ts.net/
```

Run `ops/hub/tailscale-serve.sh --status` (or plain `tailscale serve status`)
at any time to see the exact hostname currently in effect — it is whatever
this node's MagicDNS name resolves to, not something the script invents.

## 3. Backend address — why it's a variable, not a literal

The hub's platform process binds its **tailnet IP directly**, not loopback
(this is a live-hub fact, not the roam spoke's loopback-first posture — see
`docs/specs/deploy-continuity.md` §4 and `replication-continuity.md`, both of
which reference the hub's platform socket living on its tailnet address).
`tailscale serve`'s backend target has to be the address the platform
**actually** listens on, so the script never hardcodes an IP:

```bash
SNOWLINE_HUB_BACKEND_HOST="${SNOWLINE_HUB_BACKEND_HOST:-$(tailscale ip -4)}"
SNOWLINE_HUB_PLATFORM_PORT="${SNOWLINE_HUB_PLATFORM_PORT:-8850}"
```

`tailscale ip -4` is this node's own tailnet IPv4 address — asking tailscaled
for it at run time means a re-issued tailnet IP never silently breaks this
script. Override either variable if the hub's platform is ever reconfigured
(a different port, or a loopback-plus-front posture like the spoke's) —
never edit a literal IP into the script.

## 4. Reboot survival

`--bg` is load-bearing. It backgrounds the `tailscale serve` process and
writes the mapping into `tailscaled`'s own **saved serve-config store** —
not this shell session's state. That means:

- The mapping survives this terminal closing.
- The mapping survives a `tailscaled` restart.
- The mapping survives a full **reboot** of the hub: `tailscaled` reapplies
  its saved serve config on start, before this script would ever run again.

You do not need a launchd agent, a login item, or a cron job re-running this
script after every reboot — run it once, and `tailscaled` carries the config
forward. Re-running it later (e.g. after intentionally changing the backend
port) is safe and idempotent: it just re-applies the mapping.

## 5. Funnel — explicit non-goal

This item is **tailnet-only**. `tailscale serve` alone never exposes
anything to the public internet — only devices on your tailnet (or ones you
explicitly share access with, via Tailscale's normal ACL/sharing story) can
reach this URL. `tailscale funnel` (the command that WOULD expose a serve
mapping to the public internet) is never invoked by this script, and
enabling Funnel for this surface is out of scope for this item (marker
183a6ad9) — a deliberate decision, not an oversight. If a public URL is ever
wanted, that is a separate, explicitly-scoped item: it changes the threat
model (the composed gateway currently trusts tailnet + loopback peers only,
`SNOWLINE_TRUSTED_CIDRS`) and deserves its own review, not a flag on this
script.

## 6. Running it

On the hub, from a checkout (or copy the script anywhere — it has no
dependency on running from inside the repo):

```bash
ops/hub/tailscale-serve.sh
```

Requires `tailscaled` up and this node logged into your tailnet (same
prerequisite as `ops/roam/tailscale-serve.sh`). Output prints the resulting
serve config and the stable URL.

Check the current config without changing anything:

```bash
ops/hub/tailscale-serve.sh --status
```

Tear it down entirely (removes **all** `tailscale serve` config on this
node, not just this mapping — `tailscale serve reset` has no narrower
scope):

```bash
ops/hub/tailscale-serve.sh --reset
```

## 7. Verification from a phone or the MBP (spoke)

From a device that is on the same tailnet but is NOT the hub itself (your
phone with the Tailscale app installed, or the MBP spoke):

1. Open `https://<hub-hostname>.<tailnet>.ts.net/ui` in a browser. You
   should see the dashboard load — the same UI the hub serves directly at
   `http://<hub-tailnet-address>:8850/ui` (per §3, the hub's platform binds
   its tailnet address, NOT loopback — a loopback curl on the hub refuses).
2. Confirm the API surface too:
   `curl -fsS https://<hub-hostname>.<tailnet>.ts.net/health` should return
   the same healthy response as `http://<hub-tailnet-address>:8850/health`.
3. Turn off Wi-Fi and confirm it still works over cellular (still on the
   tailnet via Tailscale's client) — this is the actual point of a stable
   tailnet URL: reaching the hub from **anywhere**, not just the home LAN.
4. From a device that is NOT on your tailnet at all (e.g. mobile data with
   Tailscale disabled), confirm the URL does **not** resolve/connect —
   this is the Funnel non-goal (§5) holding: nothing here is
   internet-reachable.

If step 1 or 2 fails, re-check §3 (the backend host/port variables — try
`tailscale serve status` and confirm the target matches the address and
port the platform actually bound) before assuming Tailscale itself is
broken.
