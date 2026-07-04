# Replication continuity — two-instance availability

> **Status: draft.** How Snowline stays readable *and writable* when the
> primary host is unreachable: a second full instance on the owner's laptop,
> kept convergent by async, plugin-owned event replication over the tailnet.
> This is **not failover** — deploy-continuity.md §7's "multi-host HA out of
> scope" still stands; there is no shared socket, no leader election, no
> traffic flip. Design session 2026-07-04 (Sean), hardened the same day by
> two adversarial review passes; builds on the decision-event webhook bus
> (governance-plugin.md §7, `replication.py`) and the frozen monolith's
> `replication_ingest` (carve material, governance-plugin.md §9).

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
- **Authority means the seed of truth, not arbitration.** Both sides keep
  every record; the rare conflicting pair (e.g. a supersession race against
  oneself across the partition) resolves by a rule both sides compute
  identically (§6), and the conflict is logged loudly — never silently
  dropped. The primary's authority is operational — it is the seed (§7) and
  the always-on home — not a conflict arbiter.

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
- **Per-source identity + ordered `seq`** — every event is totally ordered
  within its source **stream**, giving idempotent, resumable ingest — with
  one structural correction to how the bus allocates `seq` today (§3.2).
- **Retry + dead-letter** — with one change needed (§3.1).

What v1 adds is the **ingest half and the generalization**:

- Carve `replication_ingest` from the frozen monolith into the **plugin SDK**
  alongside a generalized emit module. The SDK owns the *envelope* mechanics —
  outbox table + delivery loop, signature verify, the per-stream watermark
  table and contiguous-apply gate (§3.2), origin suppression (§3.2) — and the
  plugin supplies the domain **apply function** (payload in, idempotent local
  write out). A plugin opts in by adopting the SDK module, not by rewriting
  replication (§4).
- `SNOWLINE_REPLICATION_SOURCE_ID` becomes instance-qualified:
  `<instance>.<plugin>` (e.g. `roam.governance`), so streams are
  per-peer-per-plugin and a third instance later needs no schema change.

### 3.1 Dead-letter policy for replication-class subscriptions

The bus's attempt cap (`SNOWLINE_WEBHOOK_MAX_ATTEMPTS`, default 5) treats
sustained unreachability as failure. For a replication peer, **being down for
two weeks is normal operation**, not failure — a vacationing laptop must never
dead-letter the primary's stream (or vice versa). Replication subscriptions
therefore use **unbounded retry with capped backoff** — new machinery, not a
tuning change: the bus today has NO backoff at all
(`deliver_pending` retries every pending row flat on each interval tick;
`attempts` is only a counter), so this adds per-row `next_attempt_at` state
with exponential growth to a ceiling of ~the delivery interval × 10. Two
companions keep it correct:

- **Reconnect reset** — backoff is per-row, but reachability is per-PEER:
  each delivery tick opens with a cheap per-peer probe (a health touch on
  the peer's ingest), and a peer transitioning unreachable→reachable resets
  the backoff on every queued row for that peer's streams; any successful
  delivery or ingest does the same. The probe is load-bearing: a reset that
  only *reacts* to a successful delivery can never produce the first one —
  on a quiet heal with every row at the ceiling, nothing would fire for ~10
  intervals. With the probe, the next tick detects the heal and flushes the
  backlog — which is what makes §10's "within one delivery interval of
  reconnect" criterion satisfiable.
- **Dead-letter stays reserved for *rejections*** — a delivered event the
  receiver refused (bad signature, contract-version mismatch) — which
  indicate a bug, not a partition. An ORDERING refusal (§3.2's "expected
  seq N") is explicitly NOT a rejection — it is retryable by definition and
  never dead-letters. Receiver-side authentic-but-unappliable events park
  instead (§8.1).

### 3.2 Stream identity — emit-time seq, epochs, causal context

The bus today allocates `seq` per-SUBSCRIPTION at DELIVERY time
(`deliver_pending`). That is the right shape for fire-and-forget webhooks and
the wrong one for replication: delivery order is not authoring order, a
re-created subscription restarts at 1, and a watermark keyed off `source_id`
alone would reject a re-paired stream wholesale as already-seen. **This
section AMENDS the recorded bus contract** — governance-plugin.md §7 and
replication.py's carve notes (decision `97907576`, #630) document
delivery-time seq as the standing behavior; replication is the requirement
that changes it, and both records gain a pointer here when §9 item 1 lands.
v1 therefore pins the following envelope semantics (all SDK-owned):

- **`seq` is allocated at EMIT time, in the domain write's transaction** — a
  per-stream counter incremented alongside the outbox insert. The stream's
  order is fixed at authoring and survives any delivery timing, and the
  counter travels with the store in a `pg_dump` (load-bearing for §7).
- **A stream is `(source_id, epoch)`** — `source_id` = `<instance>.<plugin>`;
  `epoch` is minted at pairing and re-minted at every re-pair/re-seed. The
  receiver keys its watermark per stream, so a fresh epoch's seq restarting
  at 1 can never collide with the old epoch's watermark.
- **Contiguous apply** — the receiver applies exactly `watermark + 1`; an
  out-of-order delivery is refused with "expected seq N", and the sender's
  per-stream delivery cursor does not advance past an undelivered seq. A
  persistently failing delivery therefore *blocks its own stream* — loud and
  recoverable (§3.1, §8.1) — instead of being skipped and later discarded as
  already-seen. Ordering can never silently drop an event.
- **Causal context** — each event carries `peer_seen`: the highest seq of the
  RECEIVER's stream the author had **applied** when it authored this event
  ("I had seen your stream up to X"). One integer in the two-instance
  topology. This is what makes concurrency *computable* (§6.1) instead of
  guessed from wall clocks. Note the two counters this forces apart once
  parking (§8.1) exists: the **delivery gate** (the watermark, which parking
  advances so the stream flows) and **`applied_seq`** (the highest seq
  actually applied — what `peer_seen` reports). A parked event advances the
  gate but never `applied_seq`; conflating them would let a parked-unseen
  event masquerade as seen and silently blind §6.1's detection.
- **This envelope is contract version 2.** The fields above (epoch,
  emit-time seq, `peer_seen`) are breaking additions, and
  `check_contract_version` accepts anything ≤ its own version — so without a
  bump, a v1 peer would silently accept and misprocess a v2 event, and
  §3.1's contract-mismatch dead-letter could never fire between an old and
  new peer. `CONTRACT_VERSION` moves to 2 in BOTH pinned copies
  (`snowline_governance.contract` and the SDK's — the drift-guard test keeps
  them equal), and every new event type this spec introduces (§4 memory, §8
  scopes, governance's shadow/artifacts/specs) lands in the drift-guarded
  `EVENT_TYPES` registries the same way: both packages, one commit.
- **Origin suppression (hard rule)** — an ingest-applied write NEVER
  re-emits. The SDK apply path runs the domain write with the emit hook
  disabled; events exist only for locally-originated writes. Without this
  rule a delivered event boomerangs: the primary applies the spoke's
  decision, the outbox hook (which runs in the write's transaction) re-emits
  it on the primary→spoke subscription, and the pair trade the same event
  forever. With it, the two-instance mesh needs no forwarding rules at all —
  every author delivers to every peer directly.

## 4. Plugin opt-in — the replication contract

Replication is a **plugin capability, not a platform service** — plugins own
their stores (architecture.md §2), so only a plugin can emit and apply its
own domain events. The platform's role is limited to **declaration and
pairing**. This keeps the server thin and makes participation strictly opt-in:

**Manifest block (additive, optional):**

```json
"replication": {
  "contract_version": 2,
  "ingest_path": "/events/ingest",
  "events": ["decision.recorded", "decision.superseded"]
}
```

- `ingest_path` — where the plugin receives peers' signed events, relative to
  `base_url` (SDK-provided handler).
- `events` — the event vocabulary this plugin emits, declared so pairing can
  warn on version/vocabulary skew between the two instances' copies.
- Absent block = plugin does not replicate. **Gateway and health never read
  the block**; registration changes only by *storing* it — the manifest
  model and registry gain the field (§9 item 2; today's manifest model would
  silently drop an unknown key). It is advisory metadata the pairing step
  (§5) reads; the platform never routes events itself.

**What opting in requires of a plugin (the replication-safe checklist):**

1. **Runs on both instances** — its store exists on each side. (The private
   PM plugin qualifies: both the mini and the laptop are owner boxes, so the
   code-and-data-never-leave-the-owner posture of architecture.md §3.3 is
   preserved. Cross-tailnet registration already makes "which box" a URL
   detail.)
2. **UUID (or globally-unique) primary keys** — two sides author without
   coordination.
3. **Writes expressible as domain events with a deterministic merge** —
   append-mostly with explicit lifecycle events (record / supersede) is the
   easy shape. An in-place-update domain qualifies only as an explicit
   last-writer-wins register with tombstoned deletes (memory's shape — see
   the coverage note below). What disqualifies a store is mutation with
   neither append semantics nor a declared merge rule.
4. **Idempotent apply** — re-delivery and replay are no-ops past the
   watermark; the SDK gate enforces per-stream contiguous ordering (§3.2),
   the apply function enforces semantic idempotence (e.g. INSERT … ON
   CONFLICT DO NOTHING on the event's UUID).
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
  shares a machine with (§5.1), its tailnet address to the other. The
  gateway side of this is just gateway.md §5's cross-tailnet addressing
  exercised from the spoke; the PLUGIN side is small-but-real wiring — the
  SDK's registration heartbeat is single-target today, so cross-registration
  means one heartbeat loop per target platform, each advertising its
  per-target `base_url` (§9 item 1). Tailnet up → its tools work everywhere,
  proxied; tailnet down → health marks it unreachable on the remote instance
  and only its tools route-around.
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
- **memory**: a small vocabulary (`memory.set`, `memory.forgotten`) but NOT
  a small adoption. Memory's write model today violates the checklist as-is:
  `remember` is an in-place upsert keyed on the human-chosen `name` (the
  UUID is not the dedup key) and `forget` is a hard delete. Adoption
  requires a write-model rework first: `forget` becomes a **tombstone** (so
  a late-arriving pre-forget `set` cannot resurrect the memory), and apply
  converges **per name** by the same LWW-by-event-timestamp rule as §6.
  A memory named X is *semantically* a last-writer-wins register, so
  LWW-with-tombstones is its correct convergence under checklist item 3.
- **pm (private)**: its own vocabulary, defined in its own repo against the
  SDK contract; the platform never sees the payloads' semantics.

## 5. Pairing and topology

- Each instance sets `SNOWLINE_INSTANCE_ID` (`primary` / `roam`). Instance
  identity is config, not code — a third peer is another ID.
- **Pairing is a CLI step, not an MCP surface** — but it can no longer be
  purely programmatic: subscriptions are rows in each plugin's OWN store
  (the bus's `create_subscription` has deliberately no remote surface), and
  a platform-level CLI cannot reach into governance's, memory's, and pm's
  databases. So the SDK's ingest module ships a small tailnet-gated HTTP
  **replication-admin surface** alongside `ingest_path` — create/list/retire
  inbound stream registrations and outbound subscriptions. This supersedes
  the bus's "no remote surface" posture for replication-class subscriptions
  only, and it stays OFF MCP: agents never manage plumbing. (That posture is
  RECORDED in two places — the SDK's `events.py` docstring and governance
  `replication.py`'s subscription-management note, both stating "no remote
  surface in v1" — and both gain a pointer here when the surface lands, so
  no standing doc keeps asserting the opposite.)
- `snowline replicate pair <peer-platform-url>` runs **once per pair** and
  drives both sides over that admin surface. For every plugin whose manifest
  declares `replication` on both instances, per direction, it performs the
  **secret handshake**:
  1. ask the RECEIVING plugin to register the inbound stream
     `(source_id, epoch)` — the receiver mints that epoch's shared secret,
     stores it keyed by stream, and returns it once over the tailnet
     (WireGuard-encrypted transport; never logged);
  2. create the SENDING plugin's outbound subscription (peer `ingest_path` +
     stream + that secret).
  The receiver minting means the verifying side holds the secret by
  construction — a secret that only the sender knows can never verify. The
  CLI warns on any plugin opted in on one side only or with mismatched
  `contract_version`/vocabulary.
- **Secrets, concretely.** A secret authenticates one stream for one epoch.
  Storage is a row in each plugin's own store, same posture as the bus today
  (both stores live on owner boxes; at-rest encryption is the host's
  concern, not this spec's). **Rotation** is the same handshake re-run for a
  live stream: the receiver mints a replacement, accepts old+new during the
  switch, and retires the old on the first new-signed delivery — no epoch
  change, no re-seed. A leaked secret's blast radius is forged events on one
  stream *from inside the tailnet*; rotation is the remediation.
  **Sign-time is contract, not implementation detail:** signatures are
  computed at DELIVERY time over the exact bytes POSTed (the bus's existing
  behavior) — retire-on-first-new-signed depends on it, because after a swap
  the entire queued backlog re-signs with the new secret. An implementation
  that signed at emit time would strand a partitioned peer's old-signed
  backlog past retirement and dead-letter it — delivery-time signing is part
  of the envelope contract, alongside §3.2.
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
  is trusted as `owner` today; extending the trusted set to loopback makes
  that equivalence explicit and tailscaled-independent. **Config trap:**
  `SNOWLINE_TRUSTED_CIDRS` REPLACES the default when set — state the full
  list, `SNOWLINE_TRUSTED_CIDRS="100.64.0.0/10,127.0.0.0/8,::1"`. Which
  entry is load-bearing differs by topology: behind this posture's
  `tailscale serve`→loopback front, EVERY request — the local agent and
  cross-tailnet deliveries alike — reaches the app with a *loopback* peer
  IP, so the loopback entries are what admit cross-instance traffic; the
  tailnet range matters for direct-bind setups and source-preserving
  proxies, not because deliveries arrive with `100.x` sources here.
  Dropping the loopback entries is the outage. One sharp edge: a dual-stack
  bind can report IPv4-mapped peers (`::ffff:127.0.0.1`), which match
  neither plain entry — pin the listener to one address family. Possession
  of the machine implies possession of its tailnet identity (the node key
  lives on it).
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

- Plain concurrent *appends* are not conflicts *mechanically* — both records
  exist on both sides after heal, exactly as if authored sequentially. But
  two decisions authored on opposite sides of a partition can conflict
  *semantically* (one should supersede the other) while converging cleanly;
  §6.1 exists to catch exactly that.
- For a genuine race on one object, resolution is a **pure function of the
  two events, computed identically on both sides**: last-writer-wins by
  event timestamp, `source_id` as the stable tiebreak. It must be a pure
  function — no resolution event exists to carry one side's verdict to the
  other, so any rule that depends on local state (ingest order, arrival
  time) lets the two sides pick different winners and diverge silently.
  LWW-by-timestamp assumes sane clocks (two NTP-synced owner Macs); the
  tiebreak keeps even a skewed race deterministic, and §6.1 surfaces the
  pair for human review regardless of which write won.
- **What "the same object" means, concretely.** The object is the ROW a
  domain event *mutates*, not the event itself: for supersession events it
  is decision X's supersession status, not the new decisions A and B. So
  "X superseded by A on the hub and by B on the spoke" takes BOTH paths, by
  design: the automatic LWW resolves X's status (the newer supersession
  wins) so the store converges without waiting for a human, AND §6.1 flags
  A-vs-B as concurrent siblings so the human reviews the pair. A losing
  event is **applied-then-overridden, never skipped** — its append half
  (decision B exists) must survive; only its mutation of the contested row
  yields to the winner.
- **Every resolved conflict is logged at WARNING with both event ids** — the
  volume should be ~zero; if it isn't, that is a design signal to surface,
  not noise to suppress.

### 6.1 Concurrent siblings — catching semantic conflict after a long partition

The scenario §6's rules do NOT cover: the hub↔spoke link is down for a
while, and the owner (or a second user) with access to *both* instances
records decisions on each that semantically collide — one should supersede
the other. Mechanically these are distinct UUIDs appending cleanly; without
help, both stand as live decisions and nothing says so.

Whether two decisions *actually* conflict is not machine-decidable — but
"authored concurrently, in overlapping scope, during a divergence window"
is, cheaply. So the split follows core principle #1: **detection is
mechanical, adjudication belongs to the LLM.**

- **Detection, at ingest (governance-plugin behavior).** Concurrency is
  read off the envelope's causal context (§3.2): an incoming event carries
  `peer_seen` — the highest seq of the receiver's own stream its author had
  applied when authoring. Every *locally-authored* decision whose local
  stream seq is **greater than `peer_seen`** is *concurrent* with the
  incoming event — exact, clock-free, computable at apply time. The
  collision surface is the **applicability chain, not just same-scope**:
  a decision at a parent scope governs descendants, so the incoming decision
  is checked against concurrent local decisions in any scope along either
  one's ancestors-until-isolated walk (the walk governance already performs
  for `applicable_decisions`). The walk is not local, though — governance
  resolves ancestors via the platform scope service
  (`GET /scopes/{slug}/ancestors`), so detection performs a scope-service
  round-trip at apply time and depends on platform availability. Acceptable
  because the platform is co-located on loopback (§5.1) — it shares fate
  with the instance, not the tailnet — and a scope-service outage is a
  bounded retryable apply error (§8.1), never a silent skip of detection.
  Detection runs symmetrically on both sides and is deliberately
  over-inclusive — a heuristic net, not a judgment.
- **Surfacing, as first-class state — not a log line.** Each flagged pair
  gets a `concurrent_with` marker on both decisions and appears in an
  `unreconciled` read view on the governance surface (tool + UI widget), so
  the daily-driver agent *sees* it in the flow of work. A WARNING in a log
  nobody tails on vacation is not surfacing.
- **Reconciliation is ordinary governance.** The owner (or the agent, asked
  to review the pair) resolves it the way governance already resolves
  disagreement: record a supersession — which is a normal event, replicates
  normally, and clears the flag on both sides once the supersession edge
  exists between the pair (or the pair is explicitly marked compatible).
  No new write primitive, no automatic resolution: the platform never
  guesses which decision was "right."
- **Scope of the mechanism.** This is a governance-plugin concern, not SDK
  machinery — semantic conflict is domain-specific. The SDK contract stays
  envelope-level; PM (or any opted-in plugin) defines its own analogue if
  its domain has one, with this section as the reference pattern. §6's
  premise ("one human, ~zero conflicts") weakens the day a second user
  appears — this mechanism is the guard that makes that day survivable,
  while §11's CRDT revisit trigger remains for genuinely concurrent
  multi-writer semantics.

## 7. Seeding a spoke

The bus is a **delta fabric, not an event-sourced log** — outbox rows are
pending deliveries, and history is not retained for replay. Standing up a
spoke therefore starts from a snapshot — and the ORDER is load-bearing
(**pair first, dump second**): emit only writes outbox rows for
subscriptions that exist at write time, so a write landing between a dump
and a later pairing would be in neither the snapshot nor any delivery —
lost. The procedure:

1. **Pair first** (§5): create the subscriptions and mint the stream epochs
   (§3.2). From this instant every primary write emits into the stream.
2. **Then snapshot**: `pg_dump`/restore each opted-in plugin's store (and
   the platform DB for scopes, §8). Because `seq` is allocated at emit time
   in the write's transaction (§3.2), the dumped store carries its own
   stream counter — the snapshot provably contains every event up to that
   counter's value.
3. **Scrub, then set watermarks** — the restored store is the PRIMARY's
   store, replication state and all: its outbound subscription rows
   (spoke-targeted, live secrets included — step 1 *guarantees* they're in
   the dump), pending outbox deliveries, inbound watermarks, parked events.
   Booting on those is corruption: the spoke's delivery loop would drain the
   primary's cloned outbox under the primary's identity — origin suppression
   guards the emit hook, not the delivery loop. So the seed script (never a
   manual step): read each restored emit counter — keyed by the primary's
   `source_id`, which is exactly the spoke's inbound stream — initialize the
   spoke's inbound watermark and `applied_seq` for that stream to it, then
   **truncate every cloned replication table** (subscriptions, outbox,
   watermarks, parked events) before first boot. The spoke's own outbound
   counters start fresh under its own `source_id`. Events emitted after the
   dump (seq above the counter) are waiting in the primary's outbox and
   deliver normally — the snapshot-to-stream handoff is gapless and
   exactly-once.
4. From then on the spoke tracks by events alone. Re-seeding after long
   divergence is the same procedure under a **fresh epoch** — the old
   epoch's watermarks and pending rows are retired at re-pair, so the new
   stream's seq restarting at 1 can never be rejected as already-seen.
   (Spoke-side pending outbox rows must be empty or delivered first — the
   pairing script checks.)

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
watermark does not advance past it), so ordinary scope-stream lag self-heals
on the next delivery pass rather than dropping data. Retryable is
**bounded**, though — a slug that never materializes must not stall the
stream forever (§8.1).

### 8.1 Parking — the escape hatch for poison events

Contiguous apply (§3.2) plus unbounded peer retry (§3.1) means one
permanently-unappliable event — an unknown slug that never arrives, an
apply-side bug — would otherwise freeze its stream forever and silently
strand everything behind it. So a retryable apply error gets a **bound**
(implementation-time tunable; on the order of hours of delivery passes —
generous against any real scope-stream lag), after which the event is
**parked**:

- The event moves whole into a `parked_events` table (stream, seq, payload,
  reason, parked-at) and the **delivery gate** advances past it — the stream
  flows again. `applied_seq` does NOT advance (§3.2): the event was gated
  through, not applied.
- **The sender is told.** A park response ACKs the delivery exactly like a
  success, so the sender's per-stream cursor advances to N+1; a redelivery
  of any seq at or below the delivery gate is likewise ACKed as a no-op. A
  parked event never stalls its sender — the §3.2 "cursor does not advance
  past an undelivered seq" rule reads park and no-op ACKs as delivered.
- Parking is **loud, first-class state**, surfaced like §6.1's unreconciled
  view (tool + UI widget + health signal) — never just a log line. An empty
  parked set is the standing invariant to watch.
- A parked event is **re-appliable**: fix the cause (record the missing
  scope, resolve the slug collision, ship the apply fix), then re-apply it
  from the park — apply idempotence (§4 checklist item 4) makes the replay
  safe.
- Known limit: events *behind* a parked one that causally depended on it
  may themselves park or apply with degraded meaning. Parking trades strict
  ordering for liveness and makes the trade visible; if a park cascade ever
  grows, the §7 re-seed is the clean recovery.

Dead-letter (§3.1) stays the SENDER-side terminal state for *rejections*;
parking is the RECEIVER-side terminal state for *authentic-but-unappliable*
events. The two never overlap.

## 9. v1 scope and build order

1. **SDK**: generalize `replication.py` into `snowline-plugin-sdk` emit
   module — with the §3.2 stream contract, which *changes* the emit side
   (emit-time seq + epoch in place of delivery-time seq; `peer_seen` in the
   envelope), not just relocates it; write the ingest module (per-stream
   watermark + contiguous apply, signature verify, origin suppression,
   parking §8.1, idempotent apply seam) carving `replication_ingest` from
   the monolith as read-only reference. Unbounded-retry subscription class
   with per-row backoff + reconnect reset (§3.1). The replication-admin
   surface + secret handshake (§5). Multi-target registration heartbeats
   for §4.1's cross-registered shape.
2. **Manifest**: additive `replication` block + registry storage (advisory).
3. **Governance** adopts SDK ingest (emit exists); extends event coverage to
   shadow/artifacts/specs; concurrent-sibling detection + `unreconciled`
   view (§6.1).
4. **Memory** write-model rework, THEN adoption: tombstoned `forget`,
   per-name LWW apply, `memory.set` / `memory.forgotten` events (§4
   coverage note).
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
- An applied event is never re-emitted (origin suppression, §3.2): after a
  partitioned write replicates, both outboxes go quiet — no echo.
- A delivery that keeps failing blocks only its own stream (contiguous
  apply, §3.2); when it finally succeeds the stream resumes with no event
  skipped or discarded as already-seen.
- Writes to the *same* object on both sides during a partition: both sides
  converge to the same state after heal, the resolution matches §6's rule,
  and a WARNING with both event ids was logged.
- *Distinct* decisions recorded on each side during a partition, in the same
  scope or along one applicability chain: after heal, both instances flag
  the pair as concurrent siblings and the `unreconciled` view returns it
  (§6.1); recording a supersession between them clears the flag on both
  sides. A concurrent pair in *unrelated, non-inheriting* scopes is NOT
  flagged.
- A spoke-authored scope followed immediately by a spoke-authored decision in
  it replicates in order (or self-heals via §8's retryable-unknown-slug rule).
- Pairing refuses (with a clear message) a plugin pair with mismatched
  `contract_version`, and warns on one-sided opt-in.
- After pairing, BOTH directions verify: an event signed by either sender is
  accepted by its receiver (the handshake put the secret on the verifying
  side); rotating a stream's secret is hitless — old-signed deliveries are
  accepted during the switch and refused after retirement (§5).
- `remember("x")` on both sides during a partition converges to the newer
  write on both sides after heal; a tombstoned `forget` beats an older
  `set`, and a newer `set` beats the tombstone (§4 memory note).
- Seeding per §7 loses nothing: a primary write authored between pairing and
  the dump, and another authored after the dump, each reach the spoke exactly
  once (snapshot or stream — never neither, never both applied twice).
- Re-seeding under a fresh epoch (§3.2/§7) is fully accepted — no event of
  the new stream is rejected by the old epoch's watermark.
- An event whose apply keeps failing parks after the bound (§8.1): the park
  ACKs to the sender (its cursor advances past the parked seq), the stream
  resumes, the parked view shows it, `applied_seq` does NOT advance, and
  re-applying it after the cause is fixed succeeds.
- After the §7 scrub, the spoke's first boot delivers nothing it didn't
  author: no cloned subscription, outbox row, watermark, or parked event
  survives the restore.
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
