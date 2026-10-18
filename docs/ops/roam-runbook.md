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
recover after long divergence, re-seed (§6) — or, when BOTH sides authored
history while unpaired (a spoke stood up empty), run the union (§6c).

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

## 6d. After an upgrade adds an event type — refresh the vocabulary (pm#133)

A paired stream carries only the event types the sender declared at pairing.
When an upgrade adds one, do NOT re-seed (the never-queued writes cannot be
replayed); widen the live streams instead, after BOTH sides run the new build:

```bash
# Both instances UP, on the new build. From either box (here roam):
uv run snowline replicate refresh-vocabulary http://mini.CHANGEME.ts.net:8850 \
    --local-url http://127.0.0.1:8848 \
    --local-peer-url http://roam.CHANGEME.ts.net:8848 \
    --local-instance roam --peer-instance primary --dry-run   # plan: added / current
uv run snowline replicate refresh-vocabulary http://mini.CHANGEME.ts.net:8850 \
    --local-url http://127.0.0.1:8848 \
    --local-peer-url http://roam.CHANGEME.ts.net:8848 \
    --local-instance roam --peer-instance primary
```

Each direction widens to what the sender emits AND the receiver declares; a type
only one side knows is held back (re-run after upgrading the other side). The
verb only widens, refuses a `contract_version` mismatch, and is safe to re-run.
A sender whose SDK predates the route is reported as REFUSED for that direction
(the others still widen; the verb exits 1) — upgrade it and re-run.
Then `…/parked/reapply` any events the receiver parked during the skew window.

## 6c. Stood up EMPTY — the cold-start union merge (§7.1, #98)

The scenario §5/§6 cannot cover: the mini went down, `roam` was stood up
**empty** (no recent backup) and used for days, and now the mini is back with
months of pre-outage history. **Neither store is a superset** — `pair` alone
only replicates forward in time, and a §5/§6 seed's restore would overwrite one
side's writes. The rule is absolute: **no write authored on either side is
absent after the reunion.** Use the union, never a seed:

```bash
# Both instances UP. From either box (here roam):
uv run snowline replicate union http://mini.CHANGEME.ts.net:8850 \
    --local-url http://127.0.0.1:8848 \
    --local-peer-url http://roam.CHANGEME.ts.net:8848 \
    --local-instance roam --peer-instance primary --dry-run   # plan + pairing state
uv run snowline replicate union http://mini.CHANGEME.ts.net:8850 \
    --local-url http://127.0.0.1:8848 \
    --local-peer-url http://roam.CHANGEME.ts.net:8848 \
    --local-instance roam --peer-instance primary
# The pair was paired before the outage? add --repair (see step 1).
```

What it does — one verb, the four §7.1 steps, all over the §5 admin surface:

1. **Pair the live streams first.** Unpaired → a fresh §5 pair. Already live-
   paired both ways → reused. Previously paired (old streams half-alive after
   the outage) → `--repair` retires every old stream on **both halves of both
   sides** (each instance's outbound subscriptions AND inbound registrations),
   then pairs fresh. A mixed/half-paired state refuses without `--repair`. The
   §6 re-seed preconditions do NOT apply — the union replaces the restore.
   Forward-in-time replication starts here; everything after runs under live
   traffic.
2. **Backfill the scope namespace, both directions, to completion.** Each
   side's platform walks every scope into a `<instance>.platform#backfill`
   stream (receiver-minted secret, fresh epoch); the ordinary delivery loops
   drain it. Shared slugs (roam re-created them under its own ids) **converge
   by slug** — each side keeps its own row and id, metadata goes to the newer
   `updated_at`, missing parent links heal from the peer — logged at WARNING,
   **never parked**. The tool waits for `applied_seq` == the walker's final seq
   with nothing parked before any plugin starts.
3. **Backfill each plugin, both directions** (governance, memory; any plugin
   without an enumerator is WARNED and skipped — its partition history does not
   travel until it ships one; pm's lands separately). An empty side simply
   enumerates nothing.
4. **Verify, then retire** every backfill stream on both halves. Steady state
   is again exactly one live stream per direction; the retired backfill
   streams stay listed for audit.

When it stops (it always stops LOUDLY, never skips):

- **A backfill event parked** — the tool names it. Fix the cause, then
  `POST <admin>/replication-admin/parked/reapply {source_id, epoch, seq}` (or
  `…/parked/drop` if it is resolved another way) on the instance holding the
  park, and **re-run the same command — it resumes** the open streams instead
  of opening new ones.
- **A delivery dead-lettered** — a rejection, i.e. a bug; fix, requeue
  (`…/rejected/requeue`), re-run.
- **Timeout** (`--timeout`, default 3600s per phase) — the streams stay open;
  check `GET <admin>/replication-admin/backfill` (sender: final seq, pending,
  head error) and `GET …/inbound` (receiver: `applied_seq`), then re-run.

After it completes:

- **Triage the §6.1 unreconciled view ON BOTH INSTANCES.** Every backfilled
  decision is concurrent with every locally-authored decision in an
  overlapping applicability chain (`peer_seen` 0 is the truth for pre-pairing
  writes), so a week of partition work against months of history CAN flag
  plenty — deliberately over-inclusive. Supersede the real conflicts;
  `mark_decisions_compatible` is the bulk affordance for cohorts you review
  together. A decision authored live MID-union may flag on one side only.
- **Old parks on retired pre-union streams** are superseded by the backfill
  (it re-enumerated every row those deliveries carried). The tool lists them;
  re-run with `--drop-retired-parks` to drop them once the union verified.
- **Previously-paired reunion caveat:** decisions replicated in BEFORE the
  provenance column existed read as locally authored and flag extra pairs —
  expect them and bulk-resolve with `mark_decisions_compatible`. (The cold-start
  case has none: the empty side has no replicated history.)
- **Milestones union too** (the platform stream carries scopes AND
  milestones): a milestone authored on one side during the partition exists
  on both afterwards, a same-address edit resolves per register by LWW, and
  both sides' transition logs merge — check `milestone_unreconciled` on both
  instances for any illegal converged move (§9), as after live replication.
- **Milestones with no LWW clock on EITHER side keep their local value.** Rows
  that predate milestone replication (`lww_authored_at` / `lifecycle_authored_at`
  NULL) carry no coordinate, so the union does not pick a winner between two
  such rows — each instance keeps its own and logs a WARNING. Vacuous for a
  cold-start (the empty side's rows are all stamped); for a previously-paired
  reunion with pre-migration rows on both sides, stamp them first if a
  deterministic outcome is wanted:
  `UPDATE milestones SET lww_authored_at = COALESCE(updated_at, created_at) WHERE lww_authored_at IS NULL`
  (and the same for `lifecycle_authored_at`), on the side whose values should win.
- **Shared-slug scopes: re-check `isolated` / `status` / `name` afterwards.**
  The cold-start side's re-creation of a shared slug is the NEWER write, so
  its defaults (`isolated=False`, default name) win the LWW on both sides —
  the hub's curated `isolated=True` is overwritten. Restore it with
  `update_scope` after the union; `isolated` drives `applicable_*` and the
  §6.1 ancestors walk.
- **Both sides must run #98 or newer.** The tool probes the backfill surface
  on every participant on both sides first and refuses with "upgrade the peer
  first" — do that (`snowline stack sync`) rather than working around it: a
  pre-#98 side would park every shared scope instead of converging it.
- **Quiesce writers for the first poll interval.** The tool waits one poll
  interval between pairing and the first walk so that a write whose
  transaction straddled the pairing instant lands in the live stream; a
  transaction held open longer than that is the one residual way to lose a
  write (spec §7.1 implementation notes).

**Never run `snowline replicate seed` while a union is in flight.** A seed
re-prime retires ANY active outbound targeting the spoke's ingest URL — and a
backfill outbound targets the same URL, so the prime now REFUSES (SeedError)
when it finds one rather than stranding the union mid-walk. Finish (or abandon
and retire) the union first.

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
| (not previously machine-checked) | Registration gate (#228): polls the spoke platform's `/plugins` (default 60 s, every 2 s) until every plugin is listed WITH a `replication` block — `/health` answering is not registration — and refuses naming each missing plugin and its `~/Library/Application Support/Snowline/logs/snowline-<svc>.err.log`. Then a best-effort probe of each spoke ingest URL over this box's tailnet address: an unreachable port or a gateway 502/503/504 is a WARN (likely a `tailscale serve` mirror predating the plugin — `stack sync` does not manage it), never a refusal. |
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
- **Reverse-pair failed part-way** (#228 — e.g. `participant 'pm' is not
  opted-in on the booted spoke`). `snowline replicate seed --reverse-pair`
  attempts every participant, pairs what it can, and only then fails naming
  every participant it could not pair. It is idempotent: a participant whose
  spoke→primary stream is already live on both halves is reported "already
  paired" and skipped. A half-pair (primary inbound with no matching spoke
  outbound, e.g. after a plain re-seed, or a spoke outbound with no matching
  primary inbound) is retired and re-paired — EXCEPT that it refuses to
  retire a spoke outbound still holding undelivered (pending/rejected) outbox
  rows, since a retired stream never delivers or requeues them: let the
  outbox drain or resolve the spoke's `/rejected` rows first, or pass
  `--force` to retire anyway and accept losing those writes (bootstrap-spoke
  never passes `--force`). So fix the
  named plugin (its err.log), kickstart it, and re-run
  `snowline replicate seed --config ~/.config/snowline/seed.json --reverse-pair`,
  or restrict it with `--participants pm` (comma-separated). Re-running
  `bootstrap-spoke` also works, but re-seeds first.
- **Re-pairing.** A live pair refuses a second `pair`/plain-`seed` run by
  design (§4: "pairing runs once per pair"). To rotate a secret, use the
  ordinary rotation path (not covered by `bootstrap-spoke` — it is a
  standing-pair operation, not a bootstrap one). To recover after long
  divergence, use `snowline stack bootstrap-spoke --reseed`, which is this
  runbook's §6 procedure end to end, including the fresh-epoch retirement of
  the old streams.

## 6e. Failover front — one MCP URL that survives the hub going dark (#260)

The MBP's Claude Code normally talks to the hub (the mini's gateway,
`http://100.81.176.75:8850`) so it always has the newest fixes. When the mini
loses power, work must continue against the MBP's own spoke — usually with the
operator on a phone driving a remote-control session, where "edit the MCP URL"
is not an available step. The **failover front** makes that automatic.

It is a tiny loopback-only reverse proxy on the spoke at **`127.0.0.1:8850`**
(the hub gateway's own port), stack-managed like the services: `snowline stack
sync` renders `~/Library/LaunchAgents/dev.snowline.front.plist` (it runs the
PLATFORM venv's `uvicorn --factory snowline_platform.front:create_app`, so a
platform train swap upgrades it too and sync kickstarts it alongside) and
`~/.config/snowline/front.env` (operator-owned, never clobbered — drift is
reported like the other env files). `SNOWLINE_FRONT_HUB_URL` comes from
`stack.json`'s `primary_gateway_url` (or the `http://<primary>:8850` default
bootstrap-spoke would prompt with). Post-sync and post-boot health checks
probe `http://127.0.0.1:8850/front/health` alongside the gateway's `/health`.
The hub never gets one: `stack sync` is spoke-only (decision 54447516).

**Routing.** Every request is forwarded verbatim (method, raw path, query,
body, headers minus hop-by-hop; responses stream unbuffered, so MCP's
`text/event-stream` replies pass straight through) to ONE upstream:

- the **hub** while a background probe of `<hub>/health` answers **200**
  within 2 s (a `"status": "degraded"` body is still a 200 and still
  routable — only non-200, unreachable or timeout counts as a failure);
- the **local spoke platform** (`http://127.0.0.1:8848`) otherwise.

Probes run every 5 s. Hysteresis: flip hub→spoke after **2** consecutive
failures (~10 s), back spoke→hub only after **6** consecutive successes (~30 s
stable) — a flapping hub keeps you on the spoke rather than flapping with it.
At boot the first probe picks the starting upstream directly. The choice is
per request, at request start; a request in flight when a flip happens finishes
against its original upstream, and an upstream failure on a request is a 502
naming that upstream — **never** retried against the other one (no double
writes). This is safe because every MCP surface is stateless
(deploy-continuity §2, layer 1): there is no session to orphan when the
upstream changes under a client. Every flip is logged at WARNING to
`~/Library/Application Support/Snowline/logs/snowline-front.err.log`.

**Convergence.** Writes made against the spoke during an outage converge to the
hub via the existing bidirectional pairing (replication-continuity §4, decision
0b8390f7) once it is back — the spoke→primary stream's outbox delivers them.
The front changes nothing on the wire.

### Claude Code's MCP entry (on the MBP)

Point the `snowline` MCP server at the front — the same path as the hub's,
only the host changes:

```bash
claude mcp remove snowline            # the old entry pointing at 100.81.176.75:8850
claude mcp add --transport http snowline http://127.0.0.1:8850/mcp
```

(pass the same `-s/--scope` the old entry had — e.g. `-s user` — to both.)

(or set `"url": "http://127.0.0.1:8850/mcp"` on the `snowline` server in the
MCP config by hand). Nothing else changes: the hub sees the MBP's tailnet
address as the peer exactly as before, the spoke sees loopback (`owner`), and
the caller's own headers (incl. any `Authorization`) go up unchanged.

**Dashboard.** The front forwards `/ui` too, so `http://127.0.0.1:8850/ui` on
the MBP is the hub's dashboard while the hub is up and the spoke's own while it
is not — bookmark that instead of the hub URL. (Absolute redirects from either
upstream are rewritten back onto the front.)

### Reading `/front/health`

`GET http://127.0.0.1:8850/front/health` is served by the front itself and
never proxied (`/health` IS proxied, so it stays the truthful answer of
whichever upstream served it — the `X-Snowline-Front: hub|spoke` response
header on every proxied reply says which):

```bash
curl -s http://127.0.0.1:8850/front/health | jq
# {"status": "ok", "upstream": "hub", "hub_url": "http://100.81.176.75:8850",
#  "spoke_url": "http://127.0.0.1:8848", "hub_healthy": true,
#  "last_probe_at": "...", "last_flip_at": null, "flips": 0,
#  "consecutive_failures": 0, "consecutive_successes": 42,
#  "forced": null, "fail_threshold": 2, "recover_threshold": 6}
```

`upstream` is where writes are going right now. The shape is stable and
additive-only (the standby readiness surface, item 5bb2a79c, reads it).

### Forcing an upstream (debugging)

Uncomment `SNOWLINE_FRONT_FORCE_UPSTREAM=hub|spoke` in
`~/.config/snowline/front.env` and restart the front:

```bash
launchctl kickstart -k gui/$(id -u)/dev.snowline.front
```

Forced, it never flips (the probe still runs, so `hub_healthy` stays truthful;
`/front/health` shows `"forced": "spoke"`). Comment it out and kickstart again
to restore automatic routing. The probe/hysteresis knobs
(`SNOWLINE_FRONT_PROBE_INTERVAL`, `_PROBE_TIMEOUT`, `_FAIL_THRESHOLD`,
`_RECOVER_THRESHOLD`, `_READ_TIMEOUT`) live in the same file. To run a front
by hand instead (e.g. on another port): `SNOWLINE_FRONT_HUB_URL=… snowline
front --bind 127.0.0.1:18850` — the bind is refused unless it is loopback.

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
