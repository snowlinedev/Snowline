# Replication continuity — two-instance availability

> **Status: draft.** How Snowline stays readable *and writable* when the
> primary host is unreachable: a second full instance on the owner's laptop,
> kept convergent by async, plugin-owned event replication over the tailnet.
> This is **not failover** — deploy-continuity.md §7's "multi-host HA out of
> scope" still stands; there is no shared socket, no leader election, no
> traffic flip. Design session 2026-07-04 (Sean); builds on the decision-event
> webhook bus (governance-plugin.md §7, `replication.py`) and the frozen
> monolith's `replication_ingest` (carve material, governance-plugin.md §9).

## 1. The problem, precisely

The platform's daily driver runs on one Mac mini behind the tailnet. While the
owner was on vacation, that host dropped off the tailnet — and with it *every*
capability: no decision recall, no memory, no PM. One unreachable box was a
total outage for an architecture whose whole premise is durable cross-session
context.

What continuity actually requires, in the owner's words: **the primary stays
on the Mac mini**, but **reads — and writes authored while disconnected —
happen locally** on the machine the agent is physically at. The tailnet
remains the trust boundary throughout.

## 2. Shape decision — hub-and-spoke, two full instances

Considered and rejected:

- **Cloud primary (fly.io).** Solves "home box went dark" but not the stated
  ideal: a cloud primary still needs connectivity from wherever the owner is —
  on a plane it is exactly as unreachable as the mini. It also front-loads the
  container posture deploy-continuity.md §6 deliberately deferred, and the
  fleet stays mixed regardless (the private PM plugin lives on owner boxes by
  design; walkthrough-mcp needs macOS `simctl`). **Deferred, not dead**: the
  design below treats an instance as "a full stack at a tailnet address," so a
  fly node later is *just another peer*, not a rearchitecture. Revisit
  triggers: a second daily-driver user; wanting a third always-on replica;
  the Linux move that deploy-continuity §6 already names.
- **Postgres streaming replica on the laptop.** Read-only — no offline
  authoring — and promote/failback is manual and error-prone. Half the ask.
- **Symmetric multi-master.** Conflict machinery (CRDTs, vector clocks) the
  write pattern doesn't need: one user, append-mostly domains, UUID keys.

Chosen: **hub-and-spoke over the existing event bus.**

- **Two named instances**, each a *complete* Snowline: platform + its plugins
  + their own Postgres stores. The mini is `primary` (authoritative); the
  laptop is a **roaming spoke** (working name `roam`).
- **Reads on the spoke are always local** — its agent points at its own
  gateway (`localhost:8850`), tailnet up or down.
- **Writes on the spoke are provisional**: recorded locally so sessions keep
  working, queued in the plugin's transactional outbox, delivered to the
  primary when the tailnet returns. Ingested by the primary → authoritative.
- **Authority means ordering, not veto.** Both sides keep every record; on
  the rare conflicting pair (e.g. a supersession race against oneself across
  the partition) the primary's resolution wins deterministically and the
  conflict is logged loudly — never silently dropped (§6).

### 2.1 Layer 0 — the primary must actually be always-on (standing posture)

No topology survives an ops gap on the hub. Recorded as prerequisite posture,
not this spec's build: disable sleep (`pmset`), run `tailscaled` as a system
daemon (not the login-session menu-bar app), auto-restart after power loss,
and an external dead-man's switch (a cron pinging a hosted healthcheck, so a
silent disconnect pages the owner instead of waiting to be discovered).

## 3. The replication fabric — the event bus, generalized

The governance webhook bus already *is* the emit half of a replication
protocol, carried from the monolith for exactly this purpose:

- **Transactional outbox** — a pending delivery row written *in the domain
  write's transaction* (`emit_decision_event`). Offline authoring needs no new
  machinery: an unreachable peer just means deliveries stay pending. **The
  outbox is the offline-write buffer.**
- **Signed events** — HMAC-SHA256 over the exact serialized body; the spoke
  and hub share a per-subscription secret.
- **Per-source identity + per-subscription monotonic `seq`** — the receiver
  keys a watermark off `source_id`, giving idempotent, resumable ingest.
- **Retry + dead-letter** — with one change needed (§3.1).

What v1 adds is the **ingest half and the generalization**:

- Carve `replication_ingest` from the frozen monolith into the **plugin SDK**
  alongside a generalized emit module. The SDK owns the *envelope* mechanics —
  outbox table + delivery loop, signature verify, `(source_id, seq)` watermark
  table, exactly-once apply gate — and the plugin supplies the domain **apply
  function** (payload in, idempotent local write out). A plugin opts in by
  adopting the SDK module, not by rewriting replication (§4).
- `SNOWLINE_REPLICATION_SOURCE_ID` becomes instance-qualified:
  `<instance>.<plugin>` (e.g. `roam.governance`), so a receiver's watermark is
  per-peer-per-plugin and a third instance later needs no schema change.

### 3.1 Dead-letter policy for replication-class subscriptions

The bus's attempt cap (`SNOWLINE_WEBHOOK_MAX_ATTEMPTS`, default 5) treats
sustained unreachability as failure. For a replication peer, **being down for
two weeks is normal operation**, not failure — a vacationing laptop must never
dead-letter the primary's stream (or vice versa). Replication subscriptions
therefore use **unbounded retry with capped backoff** (attempt cap disabled;
backoff grows to a ceiling of ~the delivery interval × 10). Dead-letter stays
reserved for *rejections* — a delivered event the receiver refused (bad
signature, contract-version mismatch) — which indicate a bug, not a partition.

## 4. Plugin opt-in — the replication contract

Replication is a **plugin capability, not a platform service** — plugins own
their stores (architecture.md §3.1), so only a plugin can emit and apply its
own domain events. The platform's role is limited to **declaration and
pairing**. This keeps the server thin and makes participation strictly opt-in:

**Manifest block (additive, optional):**

```json
"replication": {
  "contract_version": 1,
  "ingest_path": "/events/ingest",
  "events": ["decision.recorded", "decision.superseded"]
}
```

- `ingest_path` — where the plugin receives peers' signed events, relative to
  `base_url` (SDK-provided handler).
- `events` — the event vocabulary this plugin emits, declared so pairing can
  warn on version/vocabulary skew between the two instances' copies.
- Absent block = plugin does not replicate. **Registration, gateway, health
  are untouched** — the block is advisory metadata the pairing step (§5)
  reads; the platform never routes events itself.

**What opting in requires of a plugin (the replication-safe checklist):**

1. **Runs on both instances** — its store exists on each side. (The private
   PM plugin qualifies: both the mini and the laptop are owner boxes, so the
   code-and-data-never-leave-the-owner posture of architecture.md §3.3 is
   preserved. Cross-tailnet registration already makes "which box" a URL
   detail.)
2. **UUID (or globally-unique) primary keys** — two sides author without
   coordination.
3. **Writes expressible as domain events** — append-mostly with explicit
   lifecycle events (record / supersede / forget), not in-place mutation.
4. **Idempotent apply** — re-delivery and replay are no-ops past the
   watermark; the SDK gate enforces `(source_id, seq)` ordering, the apply
   function enforces semantic idempotence (e.g. INSERT … ON CONFLICT DO
   NOTHING on the event's UUID).
5. **No hard cross-plugin FKs** — already the platform rule (soft scope
   references); replication is per-plugin, so a hard FK to another store
   could not be guaranteed convergent.

**A plugin that can't (or won't) opt in degrades alone.** On the spoke it is
simply absent or `down`; the gateway already route-arounds down plugins
(gateway.md §4) and the composed surface loses only that plugin's tools —
per-plugin degradation, never whole-platform. Worst case for a private plugin
that stays single-home: this spec is its *reference* for adopting the SDK
modules later, and until then it accepts hub-only availability.

### 4.1 Non-replicating plugins — registration is per-instance

Each instance is a complete Snowline with its **own registry**; "registered"
is never a global fact. A plugin that doesn't replicate still chooses, per
instance, whether to appear on that instance's surfaces. Two sanctioned
shapes:

- **Single-home, cross-registered** — one process on one machine, registered
  (and heartbeating) with *every* instance that should compose it,
  advertising the right `base_url` per target: loopback to the platform it
  shares a machine with (§5.1), its tailnet address to the other. This is
  just gateway.md §5's cross-tailnet addressing exercised from the spoke.
  Tailnet up → its tools work everywhere, proxied; tailnet down → health
  marks it unreachable on the remote instance and only its tools route-around.
- **Per-machine, locally registered** — for a plugin whose "store" is the
  machine itself (walkthrough-mcp: the local `simctl` simulators), run an
  independent instance on each Mac, each registering only with its local
  platform over loopback. Nothing replicates because nothing is shared; the
  spoke's copy works fully offline against its own machine.

Which shape a machine-bound plugin takes is *its* semantic call — "drive the
hub machine's simulators" is the first; "drive the simulators wherever I am"
is the second — decided in the plugin's repo, not here. Both compose without
platform changes.

**Event coverage is the real per-plugin work.** The bus today emits only
`decision.recorded` / `decision.superseded`. Full-store convergence means
each opted-in plugin covers its write surface with events:

- **governance**: decisions (exists) + shadow graph, artifacts, specs — the
  gap to close, one event type per lifecycle write.
- **memory**: `memory.recorded` (remember) and `memory.forgotten` (forget) —
  a small vocabulary; recall/digest/list are reads.
- **pm (private)**: its own vocabulary, defined in its own repo against the
  SDK contract; the platform never sees the payloads' semantics.

## 5. Pairing and topology

- Each instance sets `SNOWLINE_INSTANCE_ID` (`primary` / `roam`). Instance
  identity is config, not code — a third peer is another ID.
- **Pairing is a CLI step, not an MCP surface** — subscription management is
  deliberately programmatic (replication.py's standing note). A
  `snowline replicate pair <peer-base-url>` script, run once per side:
  for every plugin whose manifest declares `replication` *on both instances*,
  it creates the cross-subscriptions (this side's bus → the peer plugin's
  `ingest_path`) with a generated shared secret, and warns on any plugin
  opted in on one side only or with mismatched `contract_version`/vocabulary.
- Delivery flows over the tailnet exactly like every other plugin call; the
  trust gate applies unchanged. No new auth surface — the HMAC secret
  authenticates the *stream*, the tailnet authenticates the *network*.

### 5.1 Bind posture — loopback first, tailnet via tailscaled

Both instances serve their composed surfaces on **loopback**, with tailnet
exposure delegated to tailscaled. Rationale:

- **The spoke must survive tailscaled being down** — that is half its job.
  Binding only the tailscale `100.x` address makes the local agent's access
  contingent on the very daemon whose absence defines "offline"; the address
  may not even be bindable then. Loopback is the one interface guaranteed
  present. The same posture on the hub keeps mini-local sessions alive
  through a tailnet outage (the vacation scenario, again).
- **Trusting loopback widens nothing.** A local process hitting the
  machine's own tailnet IP already arrives from inside `100.64.0.0/10` and
  is trusted as `owner` today; adding `127.0.0.0/8` + `::1` to
  `SNOWLINE_TRUSTED_CIDRS` makes that existing equivalence explicit and
  tailscaled-independent. Possession of the machine implies possession of
  its tailnet identity (the node key lives on it).
- **Never bind `0.0.0.0` on the roaming spoke.** The CIDR gate fails closed,
  but a wildcard bind parks a pre-auth listener on every hotel LAN the
  laptop joins. Loopback-only binds keep the untrusted-network surface at
  zero; the tailnet path is tailscaled's (`tailscale serve` TCP-forwarding
  to loopback, or a tiny front proxy — decide at implementation).
- **Layer-3 synergy.** "App on localhost, a flipper in front owning the
  tailnet address" is exactly the shape deploy-continuity.md §4 sketched
  for platform socket continuity — this posture is a step toward that
  deferred layer, not a divergence from it.

Degradation is then strictly ordered: the local path (agent → loopback)
cannot be taken down by the tailnet path; losing tailscaled costs only
cross-instance delivery and cross-tailnet plugins, and the outbox absorbs
that (§3).

## 6. Conflict policy — small by construction, loud by rule

With one authority, one human, and append-mostly domains, conflicts reduce to
one shape: **the same logical object modified on both sides during a
partition** (in practice: a supersession/forget race against oneself).

- Plain concurrent *appends* are not conflicts — both records exist on both
  sides after heal, exactly as if authored sequentially.
- For a genuine race on one object, resolution is **deterministic and
  primary-ordered**: the primary applies events in its ingest order; the
  spoke converges to the primary's outcome. Ties on the same object resolve
  last-writer-wins by event timestamp, then `source_id` as the stable
  tiebreak.
- **Every resolved conflict is logged at WARNING with both event ids** — the
  volume should be ~zero; if it isn't, that is a design signal to surface,
  not noise to suppress.

## 7. Seeding a spoke

The bus is a **delta fabric, not an event-sourced log** — outbox rows are
per-subscription pending deliveries, and history is not retained for replay.
Standing up a spoke therefore starts from a snapshot:

1. `pg_dump`/restore each opted-in plugin's store (and the platform DB for
   scopes, §8) from the primary.
2. Pair (§5); watermarks start at the primary's current `seq` — the snapshot
   already contains everything before it.
3. From then on the spoke tracks by events alone. Re-seeding after long
   divergence is the same procedure (spoke-side pending outbox rows must be
   empty or delivered first — the pairing script checks).

## 8. The scope namespace — the platform dogfoods the contract

Scopes are the shared spine every plugin references by slug; a spoke-authored
scope must exist on the hub before spoke-authored plugin writes referencing
it make sense there. So the **platform itself opts in** to the same contract
it offers plugins: `scope.created` / `scope.updated` events through the same
SDK emit/ingest modules, the same pairing step, the same watermark semantics.
Scopes are slow-changing and append-mostly (slugs are never reused;
`update_scope` is rare), so this is the contract's easiest adopter — and
building it platform-side proves the SDK seam before the PM plugin adopts it
privately. Slug collisions across a partition (same new slug authored on both
sides) fail loud at ingest and require manual resolution — acceptably rare
for a single owner.

**Ordering note:** scope events must be *ingestable before* plugin events
that reference the new slug arrive. v1 keeps this simple: plugin apply
functions treat an unknown scope slug as a **retryable** ingest error (the
watermark does not advance past it), so scope-stream lag self-heals on the
next delivery pass rather than dropping data.

## 9. v1 scope and build order

1. **SDK**: generalize `replication.py` into `snowline-plugin-sdk` emit
   module; write the ingest module (watermark table, signature verify,
   idempotent apply seam) carving `replication_ingest` from the monolith as
   read-only reference. Unbounded-retry subscription class (§3.1).
2. **Manifest**: additive `replication` block + registry storage (advisory).
3. **Governance** adopts SDK ingest (emit exists); extends event coverage to
   shadow/artifacts/specs.
4. **Memory** adopts emit + ingest (`memory.recorded` / `memory.forgotten`).
5. **Platform scopes** adopt the contract (§8).
6. **Pairing CLI** + seed procedure (§5, §7); stand up `roam` on the laptop.
7. **PM plugin** adopts privately against the published SDK — this spec is
   its behavior reference; no platform work required.

## 10. Acceptance criteria

- With the tailnet down, the spoke's gateway serves every opted-in plugin's
  reads from local data; a non-opted-in plugin's absence costs only its own
  tools.
- With **tailscaled stopped entirely** on either instance, that machine's
  agent still reaches its full local surface over loopback (§5.1); the
  trust gate accepts the loopback peer as `owner`.
- A write authored on the spoke while partitioned appears on the primary
  within one delivery interval of reconnect; re-delivery is a no-op
  (watermark verified); nothing dead-letters from unreachability alone.
- Writes to the *same* object on both sides during a partition: both sides
  converge to the same state after heal, the resolution matches §6's rule,
  and a WARNING with both event ids was logged.
- A spoke-authored scope followed immediately by a spoke-authored decision in
  it replicates in order (or self-heals via §8's retryable-unknown-slug rule).
- Pairing refuses (with a clear message) a plugin pair with mismatched
  `contract_version`, and warns on one-sided opt-in.
- Seeding per §7 yields a spoke that converges from events alone thereafter.

## 11. Out of scope

- **Failover / shared endpoint / leader election** — the spoke is never
  promoted; deploy-continuity.md §7 stands. The agent on each machine points
  at that machine's gateway; endpoint choice is client config, not platform
  behavior.
- **fly.io / a third replica** — deferred with the revisit triggers in §2.
- **Trust changes** — the tailnet remains the boundary; OAuth is a separate
  seam (architecture.md §3.5).
- **General multi-master conflict resolution** (CRDTs, vector clocks) —
  rejected in §2; revisit only if a second *human* writer appears.
- **Event-log retention / replay-from-genesis** — the bus stays a delta
  fabric; seeding is snapshot-based (§7).
