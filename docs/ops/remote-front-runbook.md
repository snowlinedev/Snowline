# Standing up the `remote-front` — Claude.ai's public MCP door

> Operational runbook for issue #120 (Stage 1) and the `ops/remote-front/`
> package. Deploy a thin, stateless public MCP endpoint on fly.io that terminates
> public TLS + MCP OAuth 2.1 for **Claude.ai custom connectors**, and
> reverse-proxies MCP to the primary gateway's chosen surface **over the
> tailnet** — only for authenticated requests.
>
> This is **not** a replication peer (Stage 2 / #83): no Snowline data lives in
> the cloud, and availability is intentionally chained to the primary (accepted
> for Stage 1). The platform is untouched — it keeps seeing ordinary
> tailnet-sourced requests and its existing trust gate applies unchanged
> (architecture §3.5 OAuth seam, exercised *outside* the platform).
>
> Companion files live in `ops/remote-front/`: `Dockerfile` + `start.sh` (the
> tailscaled sidecar), `fly.toml` (the machine), `env.example` (every knob).

The load-bearing security rule for the whole document: **the fly node must reach
exactly one port on one host.** A compromised public app is a question of *when*;
the Tailscale ACL in §4 is what bounds the blast radius to the primary's gateway
port and nothing else. Do not skip it, and do not give this node the flat
owner-level tailnet access your own machines have.

---

## 0. What this front is (and is not)

- **Is:** OAuth 2.1 authorization-code + PKCE, RFC 9728 protected-resource
  metadata, RFC 8414 AS metadata, RFC 7591 dynamic client registration, one
  fixed resource owner (a single credential — no user database), short-lived
  access tokens + refresh tokens, and a transparent Streamable-HTTP MCP proxy
  (POST + SSE, session headers passed both ways) to a configured upstream.
- **Is not:** a place any Snowline domain data is stored (only OAuth bookkeeping
  — client registrations + refresh tokens — lives in a small SQLite file), a
  replication peer, or a multi-user auth server.

The OAuth is the **official MCP Python SDK's** auth framework
(`mcp.server.auth`), not hand-rolled — see the PR / `ops/remote-front/README.md`
for the design rationale.

## 1. Prerequisites

- A fly.io account + `flyctl` logged in (`fly auth login`).
- A Tailscale tailnet with admin access (to add the ACL tag + mint an auth key).
- The **primary** already serving a dedicated gateway surface for this front over
  the tailnet (see §3). The primary keeps running exactly as today.
- This repo checked out; the package is at `ops/remote-front/`.

## 2. First launch

From `ops/remote-front/` (the build context is this self-contained member):

```sh
cd ops/remote-front
fly launch --no-deploy --copy-config --name snowline-remote-front-CHANGEME
# Create the volume that persists the OAuth store + the tailnet node identity:
fly volumes create remote_front_data --size 1 --region sea
```

Edit `fly.toml`: set `app`, `primary_region`, `REMOTE_FRONT_ISSUER_URL` (must be
your app's `https://<app>.fly.dev`), and `REMOTE_FRONT_UPSTREAM` (the primary's
tailnet surface, §3). Everything non-secret lives in `[env]`.

## 3. The exposed surface is CONFIG — compose a `remote` surface on the primary

Do **not** point the front at `main`. Claude.ai conversations ingest untrusted
web content, so write verbs reachable from this path are a prompt-injection
consideration — start read-heavy and widen deliberately. Which plugins/surfaces
to expose is your call at deploy time, expressed with the platform's existing
`SNOWLINE_SURFACES` + `SNOWLINE_SURFACE_PLUGINS` (gateway.md §2a) — **no code
change**.

On the **primary** (e.g. in `ops/roam/env.primary.example` or the launchd env),
compose a dedicated named surface, list it in BOTH envs, and restart the platform:

```sh
# Mount a `remote` surface alongside the daily-driver `main` + `shadow`:
SNOWLINE_SURFACES="main,shadow,remote"
# Compose it read-heavy — e.g. governance + memory only, no PM writes:
SNOWLINE_SURFACE_PLUGINS="main=*;remote=governance,memory"
```

That serves the composed surface at `http://<primary>.<tailnet>.ts.net:8850/remote/mcp`
— which is exactly `REMOTE_FRONT_UPSTREAM` in `fly.toml`. (Per gateway.md §2a a
constrained surface projects each allowlisted plugin's `main` tools onto it, so
you get those plugins' tools on `/remote/mcp` without editing any manifest.)

> **Verify the composition is config, not baked in:** point the front at
> `/remote/mcp` and confirm Claude.ai lists *only* that surface's tools; then
> change the allowlist + restart and see the tool list change. This is issue
> #120's "verified by pointing the front at a non-`main` surface" acceptance.

## 4. Tailscale ACL — scope `tag:remote-front` to the gateway port ONLY

**Required companion change.** In the Tailscale admin console ACL, define the tag
and grant it access to *only* the primary's gateway port. This is what makes a
compromised fly app able to reach exactly one port on one host.

```jsonc
{
  "tagOwners": {
    // You (the tailnet admin) own the tag; the auth key in §5 stamps it on the
    // fly node at join time.
    "tag:remote-front": ["autogroup:admin"]
  },

  "acls": [
    // ... your existing rules (your own machines keep their access) ...

    // The remote front may reach ONLY the primary's gateway port. Replace
    // PRIMARY-HOST with the primary's tailnet name/IP and 8850 with your
    // gateway port. No other host, no other port — not SSH, not the plugin
    // ports, not the DB.
    {
      "action": "accept",
      "src": ["tag:remote-front"],
      "dst": ["PRIMARY-HOST:8850"]
    }
  ],

  // Belt-and-suspenders: this tag never gets SSH.
  "ssh": []
}
```

> **ACL test (issue #120 acceptance):** from the running fly machine
> (`fly ssh console`) confirm the gateway port is reachable AND that any other
> host/port is refused — e.g. `curl -sS http://PRIMARY-HOST:8850/... ` succeeds
> while `curl` to the primary's SSH port or a plugin port hangs/refuses. If a
> second host/port is reachable, the ACL is too wide — fix it before exposing the
> connector.

## 5. Secrets

Three secrets, set with `fly secrets set` (never committed):

```sh
fly secrets set \
  REMOTE_FRONT_OWNER_PASSWORD="$(openssl rand -base64 24)" \
  REMOTE_FRONT_SIGNING_KEY="$(openssl rand -base64 48)" \
  TAILSCALE_AUTHKEY="tskey-auth-CHANGEME"
```

- **`REMOTE_FRONT_OWNER_PASSWORD`** — the single fixed resource-owner credential
  entered at the `/login` step. This is the only thing standing between a random
  internet visitor and your surface; make it long and random, and store it in
  your password manager (you type it once per connector authorization).
- **`REMOTE_FRONT_SIGNING_KEY`** — HMAC key that signs access tokens. Keeping it
  stable across restarts keeps issued access tokens valid; if it's lost/rotated,
  clients simply refresh (refresh tokens live in the store, not under this key),
  so a rotation costs nothing worse than one silent refresh round-trip.
- **`TAILSCALE_AUTHKEY`** — mint in the Tailscale admin console as a **tagged**
  key carrying `tag:remote-front` (Settings → Keys → Generate auth key → add the
  tag; reusable + ephemeral is fine). This is what places the node under the §4
  ACL. A key WITHOUT the tag would join with your owner-level access — do not do
  that.

## 6. Deploy

```sh
cd ops/remote-front
fly deploy
```

`start.sh` brings up `tailscaled` in userspace-networking mode, joins the tailnet
as `tag:remote-front`, exposes an outbound HTTP proxy, and starts the app with
`HTTP(S)_PROXY` pointed at it — so the app's ordinary httpx calls to
`REMOTE_FRONT_UPSTREAM` traverse the tailnet without the app importing tailscale.
Confirm it's up:

```sh
curl -s https://<app>.fly.dev/.well-known/oauth-authorization-server | jq .issuer
curl -si https://<app>.fly.dev/mcp | grep -i www-authenticate   # expect a 401 + Bearer
```

## 7. Add it in Claude.ai (connector walkthrough)

1. Claude.ai → **Settings → Connectors → Add custom connector**.
2. **URL:** `https://<app>.fly.dev/mcp` (the resource URL — note the `/mcp`).
3. Claude.ai performs the discovery walk automatically: it hits `/mcp`, gets the
   `401 WWW-Authenticate` pointing at
   `/.well-known/oauth-protected-resource/mcp`, follows it to the AS metadata,
   and **dynamically registers** (RFC 7591). No client id/secret to paste.
   - **Manual fallback:** if you prefer/needed, use Claude.ai's connector
     *advanced settings* to paste a client id/secret. Register one first with a
     `POST /register` (see `env.example` / the test helper) and enter the
     returned `client_id` + `client_secret`.
4. Claude.ai opens the **authorize** URL in your browser → the front's `/login`
   page. Enter the `REMOTE_FRONT_OWNER_PASSWORD`. On success you're bounced back
   and Claude.ai completes the PKCE token exchange.
5. Claude.ai now lists the composed surface's tools. Tool calls round-trip:
   Anthropic cloud → fly front → tailnet → primary gateway → plugin, streaming
   back (SSE survives both hops).

Idle past the access-token lifetime? Claude.ai refreshes silently — no re-auth.
Redeploy the fly app? The connector keeps working (registrations + refresh tokens
are on the volume) — no re-adding.

## Acceptance — the issue-#120 criteria and how to check each

- **End-to-end connector add** — §7 completes discovery → DCR (or manual) → PKCE
  → tokens and Claude.ai lists the surface's tools.
- **Round-trip + SSE** — call a tool from Claude.ai; the response streams back.
- **Spec-correct 401s** — `curl -si .../mcp` (no/garbage/expired bearer) returns
  a `401` with `WWW-Authenticate: Bearer ... resource_metadata="..."`; nothing
  reaches the tailnet (covered by `tests/test_proxy.py`).
- **ACL containment** — the §4 ACL test: the fly node reaches only the gateway
  port.
- **Clean upstream errors, not hangs** — stop the primary / sever the tailnet;
  `.../mcp` returns a `502 upstream_unavailable`, and recovers automatically when
  the path heals (`tests/test_proxy.py::...clean_502...`).
- **Token refresh** — leave the connector idle past the access-token TTL; it
  recovers with no manual re-auth (`tests/test_oauth_flow.py`).
- **Restart survives** — `fly apps restart`; the connector still works, no
  re-add (`tests/test_store_and_config.py::test_sqlite_store_survives_restart`).
- **Surface is config** — §3's verify step.

## Manual steps that remain (not verifiable in the build sandbox)

The app + OAuth + proxy are fully covered by `ops/remote-front/tests` against a
mock upstream. The following need the real fly account + tailnet and are the
operator's to perform, verified by the criteria above:

1. **`fly launch` / `fly deploy` / `fly volumes create`** and the three
   `fly secrets`.
2. **The Tailscale ACL** (§4) + a **tagged auth key** (§5), and the ACL
   containment test from `fly ssh console`.
3. **Composing the `remote` surface on the primary** (§3) and the
   config-not-baked-in verification.
4. **The Claude.ai connector add** (§7) end to end from Anthropic's cloud.
