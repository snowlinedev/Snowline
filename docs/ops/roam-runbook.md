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

The load-bearing rule for the whole document: **§7's ordering is not advisory.**
Prime → dump → scrub → inject → boot → reverse-pair exists in that order because
earlier drafts lost data without it. `snowline replicate seed` enforces it; do
not hand-run the steps out of order.

---

## 0. Prerequisites

- Both machines on the same tailnet, `tailscaled` up, each logged in.
- Postgres on each machine with the three databases (`snowline_platform`,
  `snowline_governance`, `snowline_memory`). The apps auto-migrate to head on
  boot.
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

This maps each service's loopback port **1:1 onto the same tailnet port**
(`tailnet:8848→127.0.0.1:8848`, `:8801→:8801`, `:8802→:8802`). The port-preserving
1:1 mapping is what the pairing CLI relies on when it rewrites a peer plugin's
loopback `base_url` onto the peer's tailnet host (§4.1) — governance at loopback
`:8801` is reachable at `<host>.tailnet:8801`.

Start the services (per instance), using the launchd agents or by hand:

```bash
# by hand, for the drill (each in its own shell, or backgrounded):
SNOWLINE_ENV_FILE=~/.config/snowline/env.roam ops/roam/run-service.sh platform
SNOWLINE_ENV_FILE=~/.config/snowline/env.roam ops/roam/run-service.sh governance
SNOWLINE_ENV_FILE=~/.config/snowline/env.roam ops/roam/run-service.sh memory
# under launchd (survives crashes/reboots): install the ops/roam/launchd/*.plist
```

Degradation is then strictly ordered: the local path (agent → loopback) cannot
be taken down by the tailnet path; losing tailscaled costs only cross-instance
delivery, and the outbox absorbs that.

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
uv run snowline replicate pair http://mini.CHANGEME.ts.net:8848 \
    --local-url http://127.0.0.1:8848 \
    --local-instance roam --peer-instance primary
```

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
# Steps 1-3: prime the primary->spoke stream, pg_dump/restore each store,
# scrub every cloned replication table (NOT the stream counters), inject the
# spoke's inbound registration.
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
2. **Dump + restore.** `pg_dump -Fc` each store, `pg_restore --clean
   --if-exists` into the spoke. The emit-time `seq` counter travels in the dump,
   so the snapshot provably contains every event up to that counter.
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
   `seed-config.example.json` with your tailnet hostnames/IPs and DB
   credentials.
