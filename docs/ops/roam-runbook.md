# Standing up `roam` — the roaming spoke

> Operational runbook for replication-continuity.md §5/§5.1/§7 and issue #82.
> Stand up a second full Snowline instance (`roam`, on the laptop) as a spoke of
> the always-on hub (`primary`, on the Mac mini), pair them, and seed the spoke
> from a primary snapshot. This is **not failover** (§11): the spoke is never
> promoted; each machine's agent points at that machine's own gateway.
>
> Companion files live in `ops/roam/`: `env.roam.example` / `env.primary.example`
> (environment), `tailscale-serve.sh` (tailnet exposure), `run-service.sh` +
> `launchd/` (service supervision), `seed-config.example.json` (the seed input).
>
> **This manual drill has a packaged alternative as of macOS distribution
> item b70b0359: `curl … | sh` (install.sh) then `snowline stack sync --role
> spoke` builds every service's venv, renders this SAME env/plist posture
> from packaged templates, and health-checks the gateway — one command
> instead of §§0-3 below. This runbook stays the reference for what that
> command automates, the pairing/seeding steps it deliberately does NOT do
> (§4-6, WRAPPED — not replaced — by the guided `snowline stack
> bootstrap-spoke` step, item 71317cd6 / §6b below, per
> macOS-distribution.md §6), and the manual two-machine drill.**

The load-bearing rule for the whole document: **§7's ordering is not advisory.**
Prime → dump → scrub → inject → boot → reverse-pair exists in that order because
earlier drafts lost data without it. `snowline replicate seed` enforces it; do
not hand-run the steps out of order.

---

## 0. Prerequisites

- Both machines on the same tailnet, `tailscaled` up, each logged in.
- Postgres on each machine with the **four** databases (`snowline_platform`,
  `snowline_governance`, `snowline_memory`, `snowline_pm` — pm joins the spoke
  drill as of macOS distribution spec §7/§8, item b70b0359; this runbook
  predates pm on the spoke). The apps auto-migrate to head on boot.
- **Each Postgres stays loopback-only** — `listen_addresses = 'localhost'`, no
  tailnet bind, no `pg_hba` entry for `100.64.0.0/10`, not even with password
  auth (governance decision 1a83031c). Seeding needs no cross-machine Postgres
  access: the primary dumps itself and serves the archive over its own
  replication-admin surface (§5 step 2). `pg_dump`/`pg_restore` must be on the
  PATH of the machine whose database they touch — the primary for the dump, the
  spoke for the restore.
- The `snowline-pm` checkout as a sibling of the platform checkout (the same
  layout `release/components.json` assumes: `../snowline-pm`) — `run-service.sh
  pm` runs from there, not the platform repo.
- The `snowline` CLI available (it ships with the platform package —
  `uv run snowline ...` from the checkout, or `pip install -e .` puts `snowline`
  on the PATH).
- The full replication stack deployed on **both** instances (SDK #77, manifest
  #78, governance #79, memory #80, scopes #81 — this issue, #82, composes them).

## 1. The trusted-CIDR list — state it IN FULL

`SNOWLINE_TRUSTED_CIDRS` **replaces** the default when set (§5.1 config trap).
As of issue #93, both the platform (`config.DEFAULT_TRUSTED_CIDRS`) and the SDK
admin surface default to the full tailnet + loopback set below when the env is
unset, so a bare first boot no longer 403s loopback deliveries or pairing-CLI
calls. **Setting it explicitly is still recommended** for clarity and for
anyone reading the deployment config without also reading the source — spell
out every trusted range on **every process on both instances**:

```
SNOWLINE_TRUSTED_CIDRS="100.64.0.0/10,127.0.0.0/8,::1"
```

- `100.64.0.0/10` — the tailnet (CGNAT) range.
- `127.0.0.0/8` and `::1` — IPv4 + IPv6 loopback.

**Why loopback is not optional here.** Behind the `tailscale serve → loopback`
front (§2 below), *every* request — the local agent and cross-instance
deliveries alike — reaches the app with a **loopback** peer IP. So the loopback
entries are what admit cross-instance traffic; dropping them is the outage —
and because `SNOWLINE_TRUSTED_CIDRS` REPLACES rather than extends the default,
an explicit setting that omits loopback is just as much an outage as it was
before the default changed. Pin listeners to one address family (loopback-only
bind, §2) so a dual-stack `::ffff:127.0.0.1` peer can't dodge the gate.

## 2. Bind posture — loopback first, tailnet via tailscaled (§5.1)

Every service **binds loopback only** (`run-service.sh` passes `--host
127.0.0.1`). **Never `0.0.0.0` on the laptop** — a wildcard bind parks a
pre-auth listener on every hotel LAN it joins. The tailnet path is tailscaled's:

```bash
# on EACH instance (primary and roam):
ops/roam/tailscale-serve.sh
```

This exposes **only the platform's port** on the tailnet
(`tailnet:8848→127.0.0.1:8848`). Plugin ports (`8801/8802/8803`) are NOT
mirrored and must not be: a peer reaches a plugin's replication surfaces
**through this instance's gateway** at `/via/<plugin>/…`, which proxies to the
plugin's loopback bind (spec §4.1, decision 0b8390f7). The pairing CLI and the
seed address peers that way automatically; you declare nothing. (On the hub the
platform binds its tailnet address directly — `ops/hub/tailscale-serve.sh` adds
the HTTPS front on top — so the hub needs no TCP mirror at all.)

> **Upgrading an instance paired under the OLD posture** (streams whose
> `target_url` dials `<peer>:8801/8802/8803`): there is no admin verb to
> repoint a live stream, so re-seed the spoke under a fresh epoch (§6) — or
> re-pair after retiring the old streams — BEFORE switching the old per-port
> mirror off. Off first wedges every old stream in backoff.

> **If a plugin is fronted some other way** (a distinct host, a non-gateway
> front): the plugin declares its peer-reachable address as
> `advertised_base_url` on its manifest `replication` block (spec §4.1), which
> pairing then prefers verbatim. The platform's own scope stream needs nothing
> here: a peer discovers it AT its base URL.

Start the services (per instance), using the launchd agents or by hand:

```bash
# by hand, for the drill (each in its own shell, or backgrounded):
SNOWLINE_ENV_FILE=~/.config/snowline/env.roam ops/roam/run-service.sh platform
SNOWLINE_ENV_FILE=~/.config/snowline/env.roam ops/roam/run-service.sh governance
SNOWLINE_ENV_FILE=~/.config/snowline/env.roam ops/roam/run-service.sh memory
SNOWLINE_ENV_FILE=~/.config/snowline/env.roam ops/roam/run-service.sh pm
# under launchd (survives crashes/reboots): install the ops/roam/launchd/*.plist
```

Degradation is then strictly ordered: the local path (agent → loopback) cannot
be taken down by the tailnet path; losing tailscaled costs only cross-instance
delivery, and the outbox absorbs that.

> **`pg_dump` under launchd.** The primary's snapshot route execs `pg_dump`
> from the *service's* environment, and a launchd-run service does not
> inherit your shell PATH. The hub's hand-written plists add
> `/opt/homebrew/bin`; a packaged instance's plists set no PATH, so the SDK
> falls back to the known Homebrew kegs and, failing that, to
> `SNOWLINE_PG_BIN=<dir containing pg_dump>` from the service's env file.

## 3. Primary standing posture (§2.1) — do this once on the mini

No topology survives an ops gap on the hub. On the primary:

- `sudo pmset -a sleep 0 disablesleep 1` — never sleep.
- Run `tailscaled` as a **system daemon**, not the menu-bar login app.
- `sudo pmset -a autorestart 1` — auto-restart after power loss.
- An external **dead-man's switch**: a cron pinging a hosted healthcheck
  (healthchecks.io etc.) so a silent disconnect pages you.

## 4. Pair (a fresh spoke that shares no history yet)

> Skip to §5 if you are STANDING UP a spoke from the primary's data — seeding
> primes the forward direction itself. Use bare `pair` only when both instances
> already hold convergent data (e.g. two empty instances in the drill) and you
> just need the streams opened.

From the roam laptop:

```bash
uv run snowline replicate pair http://mini.CHANGEME.ts.net:8850 \
    --local-url http://127.0.0.1:8848 \
    --local-peer-url http://roam.CHANGEME.ts.net:8848 \
    --local-instance roam --peer-instance primary
```

`--local-peer-url` is THIS instance's gateway as the PEER reaches it: the
reverse (primary→roam) streams are pointed at `<that>/via/<plugin>/…`
(decision 0b8390f7). Leave it out only for a same-box drill — the command
warns, and the reverse streams then target this instance's loopback.

What it does (§5): for every participant opted into replication on **both**
instances (each replicating plugin, plus the platform's own scope stream), it
runs the **receiver-mints-secret** handshake in **both directions** — the
receiver registers the inbound stream and mints the secret; the sender creates
its outbound subscription carrying that secret, with `peer_seen` wired to the
reverse stream. It **warns** on a one-sided opt-in (a plugin present with a
replication block on one side only) and **refuses** a pair whose declared
`contract_version`s differ (upgrade the lagging SDK first). `--dry-run` prints
the plan (warnings/refusals) without touching the wire.

Pairing runs **once per pair**; a re-run refuses (the receiver already holds an
active inbound stream from the sender). To change a live secret, rotate; to
recover after long divergence, re-seed (§6).

## 5. Seed a spoke from the primary (§7 — order load-bearing)

Fill `ops/roam/seed-config.example.json` → a private `seed.json`. Then, from the
roam laptop, with the **primary up** and the **spoke NOT yet serving writes**:

```bash
# Steps 1-3: prime the primary->spoke stream, fetch each store's snapshot from
# the primary and pg_restore it locally, scrub every cloned replication table
# (NOT the stream counters), inject the spoke's inbound registration.
uv run snowline replicate seed --config seed.json
```

Then **boot the spoke** (start its platform + plugins, §2), and:

```bash
# Step 4: pair the reverse (spoke->primary) direction the ordinary way.
uv run snowline replicate seed --config seed.json --reverse-pair
```

The order the tool enforces, and why each step exists:

1. **Prime first (before the dump).** The seed creates the *primary's* outbound
   subscription with a script-minted secret+epoch — the one exception to
   receiver-mints (§5): the spoke's store doesn't exist yet, so the script plays
   the receiver, carries the secret (never logged), and injects it in step 3.
   From this instant every primary write emits into the stream, closing the gap
   where a write between dump and a later subscription would be lost.
2. **Snapshot + restore.** The seed POSTs a signed request to each participant's
   `…/replication-admin/snapshot` on the **primary**; the primary runs `pg_dump
   -Fc` against its OWN database and streams the archive back, and the seed
   `pg_restore --clean --if-exists`s it into the spoke. Nothing dials the
   primary's Postgres over the tailnet — it is loopback-only (decision
   1a83031c). The request is signed with the secret step 1 just minted, and the
   route serves only a caller holding an **active** primed subscription's
   secret, so step 1's precedence is mechanical: **no prime, no snapshot**. (A
   404 from that route means either "no such stream" or "bad signature" — the
   route deliberately cannot be probed to tell them apart.) The emit-time `seq`
   counter travels in the dump, so the snapshot provably contains every event up
   to that counter.
3. **Scrub, then set watermarks.** Read the restored emit counter (keyed by the
   primary's `source_id` = the spoke's inbound stream) → initialize the spoke's
   inbound watermark/`applied_seq` to it; **truncate every cloned replication
   table EXCEPT `replication_stream_counters`**; write the spoke's inbound
   registration (the receiver's handshake half, replayed after the restore).
   Booting on the cloned outbox/subscriptions would drain the primary's outbox
   under the primary's identity — origin suppression guards the emit hook, not
   the delivery loop.
4. **Boot, then reverse-pair.** The spoke authored nothing before boot, so
   spoke→primary needs no pre-dump half — it pairs by the ordinary handshake
   (primary mints, as receiver).

After this, the spoke converges by events alone. A primary write authored
between priming and the dump arrives as a no-op **duplicate** (it is already in
the snapshot); a write authored after the dump arrives via the **stream** —
exactly once, never neither, never both.

## 6. Re-seed after long divergence (fresh epoch)

Re-seeding is the same procedure under a **fresh epoch**, but two preconditions
are checked first (both, always):

```bash
uv run snowline replicate reseed-check --config seed.json   # check only
uv run snowline replicate seed --config seed.json --reseed  # check + retire + re-seed
# then boot + `--reverse-pair` as in §5.
```

- **(a) the spoke's outbox is empty/delivered** — no undelivered spoke→primary
  writes; AND
- **(b) the primary's parked set for the spoke's streams is empty** — no
  spoke-authored event the primary received but couldn't apply.

Both are required because **a park ACKs as delivered** (§8.1): an empty outbox
does *not* imply the spoke's writes were applied on the primary. Re-seeding over
an unresolved park would overwrite the spoke's only applied copy of that write.
Resolve parks (fix the cause, re-apply from the parked view) before re-seeding.

## 6a. Rolling back a packaged-channel update (macOS distribution spec §5)

`snowline stack sync --train vPrev` is the rollback command for a spoke
installed via the packaged channel (item b70b0359) — same code path as an
ordinary sync, just naming an older train. Two cases:

- **Code-only rollback (no schema migration in the delta being undone).**
  The symlink swap IS the rollback: `current` repoints at `vPrev`'s venv,
  changed services kickstart, done. `sync` also does this **automatically**
  when a `--auto` upgrade's post-upgrade health check fails and no migration
  crossed in the delta — see the work item body's auto-upgrade failure
  posture.
- **Rollback across a schema migration.** boot-migrate is **forward-only by
  design** (§3) — the database does not roll back with the code. `sync`
  refuses to auto-revert this case (leaving state as-is and reporting loudly)
  precisely because rolling the code back against an already-migrated
  database is broken, not because reverting is unsafe in the abstract. On a
  spoke this is recoverable — but by the **re-seed protocol** (§6 above), not
  a bare seed: drain the outbox, verify the primary's parked set is empty (a
  park ACKs as delivered — reseeding over an unresolved park loses that
  write), then `reseed-check` + `seed --reseed` under a fresh epoch. This is
  the accepted cost of keeping migrations forward-only (macOS distribution
  spec §5); there is no cheaper undo for a migration that already ran.

## 6b. Packaged spoke (`snowline stack bootstrap-spoke`, item 71317cd6)

`snowline stack bootstrap-spoke [--dry-run] [--reseed]` is the guided,
packaged-channel equivalent of §§4-6 above — it WRAPS `snowline replicate
seed`/`reseed-check` verbatim (never reimplements them) and drives the same
ordering §7 states, plus the local ops (kickstart, health, pm role) this
runbook's manual drill leaves to the operator. It does **not** run a bare
`pair` (§4's note applies: a fresh spoke stand-up skips straight to seeding,
since `seed`'s priming step IS the forward pairing).

Mapping the manual drill to the command:

| Manual drill step | `bootstrap-spoke` equivalent |
|---|---|
| §0 prerequisites (both machines up, DBs exist) | Preconditions: refuses unless `~/.config/snowline/stack.json` exists, local services are installed (`snowline stack sync` already ran), and the local gateway is healthy. |
| "with the primary up" (§5) | Precondition: curls the primary's gateway `/health` over the tailnet before touching anything; refuses loudly if unreachable (see "primary unreachable" below). |
| Fill `seed-config.example.json` → `seed.json` by hand | Built automatically from `stack.json` (`primary_gateway_url`, `local_tailnet_address` — prompted once if missing, same posture as `sync`'s own prompts); written to `~/.config/snowline/seed.json`. No credential prompt: since item 0ebe6a70 the primary serves its own snapshot, so there is no primary-side Postgres user to ask for. |
| `snowline replicate seed --config seed.json` (§5 steps 1-3) | Run as a subprocess through the same argv, unmodified. |
| "boot the spoke" (§5, between steps 3 and 4) | Kickstarts all four services via `launchctl kickstart`, in the same order as §2's by-hand commands (platform, governance, memory, pm), then re-polls local health. |
| `snowline replicate seed --config seed.json --reverse-pair` (§5 step 4) | Run as a subprocess through the same argv, unmodified. |
| (not previously machine-checked) | Verifies `~/.config/snowline/pm.env` declares `SNOWLINE_PM_ROLE=spoke` — refuses loudly (without undoing the seed) if it says anything else. |
| §6 re-seed (`reseed-check` then `seed --config … --reseed`) | `--reseed`: runs `reseed-check` first (its failure — either precondition, outbox or parked-set — surfaces verbatim in the command's output/report, never swallowed), then `seed --config seed.json --reseed`, then boots + reverse-pairs exactly as the fresh path. |

A machine-readable record of every run (each precondition, each command,
outcome) is appended to `~/Library/Application Support/Snowline/bootstrap-history/`
(parallel to `sync`'s own `sync-history/` — see the PR that introduced this
command for why it is a parallel report rather than a reuse of `sync`'s
`RunReport`).

### Failure modes

- **Primary unreachable at install time.** The precondition check (curling
  the primary's gateway `/health` over the tailnet) runs and fails BEFORE
  any pairing/seeding command is invoked — nothing is primed, dumped, or
  touched on either side. Fix connectivity (tailscaled up on both ends, the
  primary actually running) and re-run; this is always safe to retry.
- **Seed interrupted or restarted.** `snowline replicate seed`'s priming
  step (§5 step 1 / §7) is itself idempotent against a prior partial run: it
  retires any orphaned forward subscription it left behind before minting a
  fresh epoch (`replication_seed.py`'s `_retire_orphan_forward`), so simply
  re-running `snowline stack bootstrap-spoke` after an interruption is the
  correct recovery — it is not a special case. If the interruption happened
  **after** boot + reverse-pair already succeeded (rare — bootstrap-spoke
  only reaches that point once the spoke is confirmed healthy), do not
  re-run the fresh path; use `--reseed` instead (see §6 above) rather than
  re-seeding over a partially-live pairing.
- **Re-pairing.** A live pair refuses a second `pair`/plain-`seed` run by
  design (§4: "pairing runs once per pair"). To rotate a secret, use the
  ordinary rotation path (not covered by `bootstrap-spoke` — it is a
  standing-pair operation, not a bootstrap one). To recover after long
  divergence, use `snowline stack bootstrap-spoke --reseed`, which is this
  runbook's §6 procedure end to end, including the fresh-epoch retirement of
  the old streams.

## 7. Acceptance — the §10 criteria and how to check each

Availability (verify with real tailscale — see "Manual steps" below):

- **Tailnet down**: the spoke's gateway still serves every opted-in plugin's
  reads from local data.
- **tailscaled stopped entirely**: the machine's agent still reaches its full
  local surface over loopback; the trust gate accepts the loopback peer as
  `owner`.

Replication (exercised by the automated drill, `ops/roam/`… and the test suite):

- A partitioned spoke write reaches the primary within one delivery interval of
  reconnect; re-delivery is a no-op; nothing dead-letters from unreachability.
- Pairing refuses a `contract_version` mismatch and warns on one-sided opt-in.
- Both directions verify after pairing; secret rotation is hitless.
- **Seeding loses nothing**: a write between priming and the dump, and one after
  the dump, each reach the spoke exactly once (§5).
- **Fresh-epoch re-seed** is fully accepted — no event of the new stream is
  rejected by the old epoch's watermark.
- After the scrub, the spoke's first boot delivers nothing it didn't author.

## Manual steps that remain (not automatable in this environment)

The pairing CLI and seed procedure are fully automated and drilled against real
Postgres. The following require the physical two-machine tailnet and are the
operator's to perform, verified by the criteria above:

1. **Install + run `tailscaled`** as a system daemon on both machines and run
   `ops/roam/tailscale-serve.sh` on each (this sandbox has no tailnet).
2. **The two tailnet-down availability criteria** (§10): pull the tailnet / stop
   tailscaled and confirm each machine's agent still serves its local surface
   over loopback. This can only be verified with real tailscale.
3. **Primary standing posture** (§3): `pmset`, the tailscaled system daemon, and
   the external dead-man's switch are host configuration.
4. **Fill the CHANGEME values** in `env.*.example`, the launchd plists, and
   `seed-config.example.json` with your tailnet hostnames/IPs. (`seed.json`
   carries no DB credentials any more — its only Postgres URL is the spoke's
   own local one; decision 1a83031c.)
