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

- **Reconnect reset** — backoff is per-row, but reachability is
  per-INGEST (per plugin, per peer — the granularity streams actually have):
  each delivery tick opens with a cheap probe of every ingest endpoint
  that has queued rows, and an ingest transitioning unreachable→reachable
  resets the backoff on that ingest's rows; any successful delivery or
  ingest does the same. Per-peer granularity would miss a single plugin's
  ingest healing on an otherwise-reachable peer — the same
  nothing-fires-for-~10-intervals pathology, one level down. The probe is load-bearing: a reset that
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
that changes it, and both records gain a pointer here when §9 item 1 (#77) lands.
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
  advances so the stream flows) and **`applied_seq`** — the **contiguous
  applied frontier**, the highest N such that every seq ≤ N has been
  applied. `peer_seen` reports `applied_seq`. A parked seq PINS the
  frontier even as later seqs apply past it (gate at 9 with seq 5 parked →
  `applied_seq` stays 4); a max-style "highest seq applied" would let the
  parked-unseen event masquerade as seen the moment seq 6 applies, silently
  blinding §6.1's detection — which is the whole reason the two counters
  are distinct.
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
  **Version skew on live streams is a hold, not a failure**: instances
  cannot restart atomically, so a rolling upgrade has a window where one
  peer speaks v2 and the other v1. A version-AHEAD event (peer upgraded
  first) is a RETRYABLE refusal — the sender's backlog waits out the
  receiver's upgrade — and a v2 receiver likewise refuses v1-envelope
  events on a v2-paired stream retryably, never accept-and-misprocess
  (`check_contract_version`'s ≤ rule is for consumers of a stable envelope,
  not for a stream whose keying fields changed). Dead-letter is reserved
  for envelopes invalid under every version either side has spoken. Upgrade
  both instances promptly; the held backlog is bounded by the skew window.
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
  "events": ["decision.recorded", "decision.superseded"],
  "advertised_base_url": "http://roam.tailnet:8801"
}
```

- `ingest_path` — where the plugin receives peers' signed events, relative to
  `base_url` (SDK-provided handler).
- `events` — the event vocabulary this plugin emits, declared so pairing can
  warn on version/vocabulary skew between the two instances' copies.
- `advertised_base_url` (optional) — the absolute address a **peer** instance
  reaches this plugin's replication surfaces at over the tailnet, when that
  differs from the loopback `base_url` the plugin advertises to its own
  registry (§4.1). Pairing (§5) prefers it; absent, pairing falls back to the
  port-preserving host rewrite (the addressing rule in §4.1). "advertised"
  is the §4.1 verb for a per-target `base_url`; the name says what it is — the
  address this plugin advertises for cross-tailnet reach — without baking the
  transport (`tailnet`) into the field, since the reachable front is a serve
  detail, not a contract one.
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

**Cross-tailnet replication addressing — the advertised-address rule.** A
replicating plugin advertises a **loopback** `base_url` to the platform it
shares a machine with (§5.1): it binds loopback and lets tailscaled own the
tailnet path. That loopback address is exactly right for the *local*
instance's own surfaces, but a **peer** discovering the plugin at pairing time
(§5) cannot reach a loopback address across the tailnet. Pairing resolves the
peer-reachable replication address in one of two ways, **in preference
order**:

1. **The plugin's declared `advertised_base_url`** (manifest `replication`
   block, §4) — the address a peer reaches this plugin's replication surfaces
   at, stated by the plugin itself. Pairing uses it verbatim. This is the
   principled answer whenever the serve posture is *not* a 1:1 port mirror — a
   non-1:1 port map, a path-based serve front, a distinct tailnet host —
   because only the plugin knows where it actually lands over the tailnet.
2. **Fallback: the port-preserving host rewrite.** With no
   `advertised_base_url`, pairing rewrites the loopback `base_url`'s host onto
   the peer's tailnet host and **preserves the port** — correct **only** under
   the runbook's `tailscale serve` posture that maps each loopback port 1:1
   onto the same tailnet port (`ops/roam/tailscale-serve.sh`). This keeps every
   existing pair working untouched: a deployment that has never needed
   `advertised_base_url` behaves exactly as it does today.

**Config trap:** the fallback's 1:1 assumption is silent. A deploy that fronts
services on non-matching ports, or behind a shared path prefix, will have
pairing rewrite onto a port that maps to the *wrong* service — or to nothing —
on the peer, and nothing at the CLI says so. The remedy is not a CLI flag; it
is the plugin declaring `advertised_base_url`. Declare it whenever the serve
posture is anything other than the documented 1:1 port mirror.

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
  construction — a secret that only the sender knows can never verify.
  **At most one inbound stream per `source_id` may be active** — `peer_seen`
  reports "the" active stream's applied frontier (§3.2/§6.1), which is only
  well-defined when there is exactly one — and the receiver's registration
  ENFORCES this: registering a fresh epoch while another is still active
  refuses loudly (409) until the old stream is retired, which the fresh-epoch
  re-seed procedure does (§7 step 5). The pairing CLI's own already-paired
  pre-check is a courtesy on top, not the guarantee. The
  CLI warns on any plugin opted in on one side only or with mismatched
  `contract_version`/vocabulary. It addresses each peer participant's
  replication surfaces by the §4.1 advertised-address rule: the participant's
  declared `advertised_base_url` if present, else the port-preserving rewrite
  of its loopback `base_url`.
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
(**prime the stream first, dump second**): emit only writes outbox rows for
subscriptions that exist at write time, so a write landing between a dump
and a later-created subscription would be in neither the snapshot nor any
delivery — lost. The procedure:

1. **Prime the primary→spoke stream** — the only half of pairing that must
   precede the dump. The seed script creates the PRIMARY's outbound
   subscription (epoch minted, secret generated by the script). Seeding is
   the one exception to §5's receiver-mints rule: the receiver's store does
   not exist yet, so the script plays the receiver's part — it carries the
   secret (tailnet transport, never logged, discarded after step 3) and
   injects it into the spoke in step 3. Without this split, pairing state
   written into the spoke before the restore would be wiped BY the restore,
   and the primary's dump can never supply the spoke's inbound secret (the
   primary is the sender on that stream and never holds it). From this
   instant every primary write emits into the stream.
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
   watermarks, parked events) before first boot, then **write the spoke's
   inbound registration** for the primary→spoke stream (stream, epoch, the
   script-carried secret) — the receiver's half of the handshake, replayed
   after the restore so it survives it. Nothing CLONED survives; the
   seed-written registration is not a clone. The retained emit counters are
   inert: emit allocation is `source_id`-keyed, so the spoke's own outbound
   counters start fresh under its own `source_id`, and any downstream seed
   reads only the counters matching the source instance's `source_id`,
   ignoring stale foreign rows. Events emitted after the dump (seq above
   the counter) are waiting in the primary's outbox and deliver normally —
   the snapshot-to-stream handoff is gapless and exactly-once.
4. **Boot, then pair the reverse direction.** The spoke→primary stream
   needs no pre-dump half — the spoke authors nothing before first boot —
   so it is created by the ordinary §5 handshake (primary mints, as
   receiver) once the spoke is up.
5. From then on the spoke tracks by events alone. Re-seeding after long
   divergence is the same procedure under a **fresh epoch** — the old
   epoch's watermarks and pending rows are retired at re-pair, so the new
   stream's seq restarting at 1 can never be rejected as already-seen. Two
   preconditions, both checked by the pairing script: spoke-side pending
   outbox rows are empty or delivered first, AND the primary's parked set
   for the spoke's streams is empty (resolved or re-applied) — a park ACKs
   as delivered, so an empty spoke outbox does NOT imply the spoke's writes
   were applied on the primary, and re-seeding over an unresolved park
   would overwrite the spoke's only applied copy of that write.

### 7.1 Cold-start union merge — reuniting two stores that both have unpaired history (#98)

§7 covers the superset case: a spoke born FROM the primary's snapshot. It
cannot cover the reunion case — **two instances that each accumulated history
while no subscription existed between them**, because replication moves
events and events exist only for writes made while a subscription exists.
Concrete scenario (2026-07-04, the real first-standup plan): the mini goes
down; the owner stands `roam` up **empty** (no recent backup); works on it
for days; the mini returns with months of pre-outage history. Neither store
is a superset — `pair` alone only opens streams forward-in-time, and a §7
seed's restore would overwrite one side's writes. The union rule is absolute:
**no write authored on either side is absent after the reunion.**

The shape: **backfill — manufacture the missing events.** A backfill walks
one instance's domain rows and replays each as a synthetic event through the
peer's EXISTING ingest path — the same idempotent apply seam, origin
suppression, per-name LWW, §6.1 sibling flagging, §8.1 parking. Convergence
rules already exist; the union only supplies the events that were never
emitted. Run once per direction, it is NOT #84 multi-writer machinery: still
one human, and "the other side already has this row" is the idempotent-apply
no-op, not a merge algorithm.

**The backfill stream — identity and envelope semantics (the §3.2 decisions):**

- A backfill is an ORDINARY stream with a reserved identity:
  `source_id = <live source_id>#backfill` (e.g. `mini.governance#backfill`),
  epoch minted fresh at backfill start. The `#backfill` suffix is
  load-bearing three ways: (1) it keeps the §5 single-active-stream-per-
  source invariant (#119) intact — the live stream and the backfill stream
  coexist because they are different sources, while the SAME invariant,
  applied to the backfill source id, gives "at most one backfill per
  direction at a time" for free; (2) `peer_seen`'s "the active inbound
  stream from `peer_source_id`" lookup keys on the LIVE source id, so a
  backfill stream can never contaminate live causal context by construction;
  (3) it is self-describing in every parked-event row, delivery log, and
  stream listing an operator will read during the union.
- The stream is opened by a §5-SHAPED handshake — receiver mints, secret
  carried once — via the union tool's own open verb, not the pairing CLI's
  `handshake_direction` (which always wires a live `peer_source_id`; a
  backfill subscription carries None, per the walker bullet). Both
  instances are up during a reunion, so none of §7's seed-plays-receiver
  exception applies. The stream is delivered by the ordinary delivery loop,
  gated/parked/signed like any stream, and **retired on completion** on
  both halves. Steady state after
  the union is exactly the steady state before it: one live stream per
  direction.
- The backfill subscription is created with an **EMPTY event vocabulary** —
  load-bearing, not cosmetic: `emit_event` fans a live write onto every
  active subscription whose vocabulary matches, and a backfill subscription
  declaring the live vocabulary would receive live writes interleaved onto
  the walker's counter — non-deterministic seqs, broken resume, live events
  wearing backfill causal context. An empty vocabulary makes the stream
  invisible to the emit hook; the delivery loop drains its directly-written
  rows regardless.
- `seq` is allocated at ENQUEUE time from the backfill stream's own counter,
  in **deterministic walk order** — `(authored_at, id)` per domain table,
  frozen against a bound at the pairing instant. The bound is
  **overlap-INCLUSIVE, because only omission is fatal**: a write whose
  transaction straddled the pairing commit can hold an authored-at past the
  wall-clock instant while its emit saw no subscription — absent from both
  the enumeration and the stream if the bound is a naive `<`. So the
  enumeration set is fixed from a snapshot serialized AFTER pairing opened
  (or a wall-clock bound plus a generous overlap margin); rows the live
  stream also carried simply no-op on apply. The frozen set is what makes
  an aborted backfill resume from its outbox/gate cursor over the identical
  sequence; a from-scratch re-run under a fresh epoch replays harmlessly
  regardless (apply idempotence). The walker writes outbox rows under the
  backfill subscription DIRECTLY — never through the emit hook — and the
  subscription carries `peer_source_id = None`: the walker stamps
  `peer_seen` 0 itself, and nothing may consult the live `_peer_seen`
  lookup for a backfill envelope. The walk is read-only on the sender; only
  the receiver's apply writes.
- **`peer_seen` is 0 on every backfill envelope — and 0 is the truth**, not
  a placeholder: pre-pairing writes were authored before any stream from the
  receiver existed, so their authors had provably seen none of it. §6.1
  reads this exactly right — every backfilled event is concurrent with every
  local row in an overlapping applicability chain (see the sibling note
  below).
- **Payload timestamps are the rows' ORIGINAL authored-at times, never
  import time.** They are the §6 LWW clock: memory's per-name register must
  resolve a cross-partition `remember` race identically whether the events
  arrived live or by backfill, and a backfilled tombstone must beat an older
  `set` on authored time, not on import order.
- Backfill envelopes are current-`CONTRACT_VERSION` envelopes with **no
  additional marker field — the stream identity IS the marker**: the applies
  that behave differently under a union (the scope convergence rule and the
  §6.1 detection-input changes below — nothing else) key off the envelope's
  `#backfill` source suffix, and every other apply is deliberately identical
  for live and backfilled events — idempotence and LWW neither know nor care
  how an event arrived. (One normalization: an apply that records the
  envelope's source into domain state — §6's LWW registers do — strips the
  `#backfill` suffix first, so register state stays byte-identical across
  instances whichever way the winning event arrived.)

**Enumerators — the per-plugin half.** Each replicating participant defines a
backfill enumerator next to its `apply`: a deterministic iterator over its
replicated row surface that yields `(event_type, payload)` pairs from its
EXISTING §4 vocabulary such that applying them to an empty peer reproduces
each row's current state, and applying them to a peer that already has the
row is the no-op/LWW path. Two rules with teeth: the enumeration includes
**lifecycle and tombstone state** (a superseded decision enumerates as
`recorded` + `superseded`; a forgotten memory enumerates its tombstone — a
walk of only "live" rows would resurrect deletions on the peer); and where
one event cannot carry a row's final state, the enumerator emits the minimal
lifecycle sequence rather than inventing a new event shape. An enumerator MAY
skip rows its store marks as replicated-in from the peer; correctness never
depends on it (UUID no-op), it only saves traffic.

**Scope identity — the union's one real identity decision.** Live replication
lets every instance hold the same scope UUID because scope rows replicate
from a single mint. A cold-start union breaks that: `roam`, stood up empty,
re-created the same slugs the mini already had — same slug, different id, on
both sides, for *every shared scope*. Two consequences, both settled here:

- **Slug is the cross-instance identity of a scope; scope UUIDs are
  instance-local.** On a backfill stream, `scope.created` for a slug that
  exists locally under a different id applies as **convergence** — a no-op
  that keeps the local row and id, metadata LWW'd (clock below), logged at
  WARNING — NOT the §8 `ParkNow`. Parking is the right posture for a LIVE
  same-new-slug race (a genuine surprise); on a union the collision is the
  EXPECTED case for every shared slug, and parking would flood the §8.1
  view with non-events while pinning the scope stream's frontier at zero.
  Convergence covers the PARENT edge too: the payload names the parent by
  slug, and convergence re-resolves it locally — a cold-start side that
  created `a/b/c` before `a/b` existed heals its missing parent link from
  the peer's payload, keeping the two instances' ancestor chains congruent
  (the §6.1 walk must agree on both sides). The scope enumerator emits
  `scope.created` ONLY, carrying each row's final state — never
  `scope.updated`.
- **Id divergence is permanent, so the LIVE scope stream must survive it:
  `scope.updated` applies by SLUG everywhere, wire id advisory.** Without
  this the union plants a time bomb: after retirement, every live update to
  a shared-slug scope carries the author's id, meets the peer's different
  local id, and parks — permanently, across the entire shared namespace,
  with `reapply_parked` never able to succeed (the ids never re-converge).
  So the id-keyed `ParkNow` posture NARROWS to live `scope.created`
  same-new-slug races; updates key on the slug and converge metadata by the
  LWW clock. Under live-from-genesis replication ids coincide, so this
  changes nothing observable today.
- **The LWW clock**: the scope payload carries no timestamp today, so it
  gains the row's authored/updated stamps as ADDITIVE fields —
  `.get`-tolerant on the apply side, no contract bump. The UPDATED stamp
  arbitrates metadata (falling back to the created stamp when absent); the
  created stamp is provenance only. The apply persists the WINNING EVENT'S
  updated stamp as the row's LWW coordinate — suppressing any
  `onupdate=now()` column default, which would silently rebase the
  coordinate to import time and let an older backlogged update beat a newer
  one (the discipline governance's LWW registers already embody).
- **Plugin apply resolves scope references by SLUG; the wire `scope_id` is
  advisory.** Plugin payloads carry both (`scope`, `scope_id`); the apply
  seam and §6.1's detection walk must key on the local resolution of the
  slug, never trust the wire id into a stored row or an ancestor-chain
  comparison. Under live replication the two are identical, so this costs
  nothing observable today — it is the invariant that makes id divergence
  permanently harmless rather than a latent corruption class.

**Sibling detection under a union.** With `peer_seen` 0, a backfilled event
is concurrent with every overlapping-chain local row — deliberately
over-inclusive, and under a union the honest volume: a week of partition
writes against months of pre-outage history CAN semantically collide
anywhere their chains overlap. But the live detection mechanism cannot
simply run as-is: its candidate source is the receiver's outbox toward the
author (outbox membership is what proves "locally authored, unseen by the
author"), and pre-pairing local history has no outbox rows on any live
stream — a candidate query keyed that way silently exempts exactly the rows
the union exists to reconcile. So detection changes FOR BACKFILL-STREAM
ENVELOPES ONLY (live streams keep today's outbox-based mechanism,
before, during, and after the union):

- **Candidates are locally-AUTHORED rows, discriminated by provenance.**
  Apply stamps the (suffix-stripped) origin source on every row it creates
  — an additive provenance column on sibling-flagging domains; NULL means
  locally authored. Without it, a row backfilled in five minutes ago reads
  as "never-emitted local history" and flags against its own author's next
  event — sequential same-source events masquerading as concurrent
  siblings, O(n²) within every chain. Provenance is what makes "locally
  authored" computable; the enumerator's may-skip-replicated-rows note
  stays an optimization precisely because detection no longer leans on it.
- **Detection runs only when the apply actually CREATES the row.** The live
  mechanism re-derives flags on replay by design; under a union, a UUID
  no-op (the peer already had the row — including everything shared from a
  pre-divergence era) must flag nothing, or the shared corpus itself
  becomes the flood. This is a behavior change at the apply seam, gated on
  the `#backfill` source like the candidate-set change.
- **`mark-compatible` remains the bulk affordance** for cohorts the owner
  reviews together. The reconcile pass is owner-signed-off procedure
  (2026-07-04): the tool's job is the union, the judgment stays human.

Post-union live traffic needs no special case: new events flag through the
ordinary outbox mechanism, and pre-pairing history — now present on both
sides and provenance-stamped — never re-enters the candidate set. Two
honest edges: rows replicated in during a live-pairing era that PREDATES
the provenance column read as locally authored (vacuous in the cold-start
case — the empty side has no such rows — but a previously-paired reunion
should provenance-backfill where determinable, or expect and bulk-resolve
the extra flags; runbook note); and a row authored live mid-union may flag
on one instance only (its concurrent partner arrived by backfill there and
by live stream on the other side, where it has no outbox seq) — nothing is
lost, `mark-compatible` converges both sides, but the union's triage reads
the unreconciled view ON BOTH INSTANCES, not one.

**The procedure (CLI-driven, one verb per step, §5 admin surface only):**

1. **Pair live streams first** (fresh §5 pair; for a previously-paired pair,
   retire the old streams ON BOTH HALVES OF BOTH SIDES first — each
   instance's outbound subscriptions AND inbound registrations. §7 step 5's
   helper retires only the primary's halves, because a re-seed's restore
   wipes the spoke's anyway; a union has no restore, and a spoke-side
   outbound left active would keep delivering its old backlog into a
   retired inbound — 404 `unknown_stream` is the REJECTION vocabulary, so
   the whole backlog would dead-letter one row per tick for nothing. The
   re-seed preconditions do NOT apply — a union replaces the restore.)
   Old-epoch leftovers are superseded
   by the backfill, which re-enumerates every row those deliveries carried:
   undelivered pending rows on a retired stream stay put (nothing drains a
   retired stream), and an old unresolved park can be dropped once the union
   verifies — its payload re-arrives on the backfill stream and applies (or
   re-parks loudly under the union's own identity). Forward-in-time
   replication starts now; everything from here runs under live traffic.
2. **Backfill the scope namespace, both directions, to completion** —
   `applied_seq` at the walker's final seq, parked set empty — before any
   plugin backfill starts. §8's ordering note self-heals plugin-before-scope
   lag retryably, but starting plugins after scopes converge turns a
   park-and-retry storm into a quiet pass.
3. **Backfill each opted-in plugin, both directions.** Order among plugins is
   free (no hard cross-plugin FKs, §4 checklist 5). An empty side simply
   enumerates nothing — the cold-start case degenerates to one real
   direction per store.
4. **Verify, then retire the backfill streams** on both halves. Completion =
   every enumerator exhausted, every backfill stream's `applied_seq` equal to
   its final seq, parked sets empty (or explicitly resolved), and the §6.1
   unreconciled view triaged ON BOTH INSTANCES (a mid-union live write can
   flag on one side only — see the sibling note). The retired streams stay
   for audit, like every
   retired stream.

**Acceptance (§10 additions).** No write authored on either side during the
partition is absent after the union; every cross-partition collision is
either LWW-resolved (same object) or flagged unreconciled (§6.1 siblings);
a backfilled `remember`/`forget` race resolves identically to its live
equivalent (original authored-at as the clock); a shared-slug scope pair
converges to one local row per side with no park; re-running a completed
backfill under a fresh epoch changes nothing (idempotence end-to-end); and
after retirement, steady state is indistinguishable from a §7-seeded pair.

**Build note.** SDK: the direct-enqueue walker driver, backfill stream
lifecycle (open/status/retire), the `#backfill` identity rule, and a
parked-event DROP verb (step 1/step 4 resolve old parks by dropping them
once the union verifies; today's only verbs are list/re-apply). Platform:
the union-mode scope apply (created-collision convergence on the backfill
stream, parent re-resolution by slug), the live `scope.updated`-by-slug
change, and the additive authored/updated stamps in the scope payload.
Governance: slug-keyed reference resolution in apply and §6.1 detection
where the wire `scope_id` is trusted today; the provenance column and the
backfill-gated detection changes (provenance-discriminated candidates,
created-only flagging). Plugins: one enumerator each (governance, memory,
platform scopes; PM privately, per §9 item 7). CLI: `snowline replicate
union` orchestrating the procedure above; mutual exclusion with the §7 seed
noted in the runbook (a seed re-prime retires ANY outbound matching the
spoke's ingest URL — a backfill outbound included, since it targets the
same URL). Runbook: the "stood up empty" recovery path lands beside §5/§6
when the tool ships.

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
for a single owner. (On a §7.1 backfill stream the same collision is the
EXPECTED case, not a race, and converges by slug instead of parking — slug is
the cross-instance identity of a scope. §7.1 narrows the park posture to live
`scope.created` races only: live `scope.updated` applies by slug everywhere,
because a union leaves scope UUIDs permanently instance-local and an id-keyed
update path would park every post-union update to a shared-slug scope.)

Because the platform dogfoods the contract, it also **self-describes** it. A
plugin's `contract_version`/vocabulary is discoverable at pairing time from its
manifest `replication` block in the registry (§4); the platform has no
registry entry of its own, so it exposes a small **replication self-manifest**
endpoint next to its replication surfaces — the same shape as the manifest
block (`contract_version`, `ingest_path`, `events`), tailnet-gated exactly like
the §5 admin routes it sits beside. Pairing reads it the way it reads a
plugin's block, so a skewed platform `contract_version` **refuses at pairing**
just like a plugin's — rather than the skew surfacing only later as a
delivery-time `version_hold`. The scope stream carries no peer-reachable
address distinct from the platform's own base URL (a peer discovers the
platform *at* that URL), so the self-manifest's `advertised_base_url` is
absent; the field exists for shape-parity with the plugin block.

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
  safe. Re-applying unpins the frontier: `applied_seq` advances through the
  formerly-parked seq and any contiguously-applied span beyond it (§3.2).
- Known limit: events *behind* a parked one that causally depended on it
  may themselves park or apply with degraded meaning. Parking trades strict
  ordering for liveness and makes the trade visible; if a park cascade ever
  grows, the §7 re-seed is the clean recovery.

Dead-letter (§3.1) stays the SENDER-side terminal state for *rejections*;
parking is the RECEIVER-side terminal state for *authentic-but-unappliable*
events. The two never overlap.

## 9. v1 scope and build order

1. **SDK** (#77): generalize `replication.py` into `snowline-plugin-sdk` emit
   module — with the §3.2 stream contract, which *changes* the emit side
   (emit-time seq + epoch in place of delivery-time seq; `peer_seen` in the
   envelope), not just relocates it; write the ingest module (per-stream
   watermark + contiguous apply, signature verify, origin suppression,
   parking §8.1, idempotent apply seam) carving `replication_ingest` from
   the monolith as read-only reference. Unbounded-retry subscription class
   with per-row backoff + reconnect reset (§3.1). The replication-admin
   surface + secret handshake (§5). Multi-target registration heartbeats
   for §4.1's cross-registered shape.
2. **Manifest** (#78): additive `replication` block + registry storage (advisory).
3. **Governance** (#79) adopts SDK ingest (emit exists); extends event coverage to
   shadow/artifacts/specs; concurrent-sibling detection + `unreconciled`
   view (§6.1).
4. **Memory** (#80) write-model rework, THEN adoption: tombstoned `forget`,
   per-name LWW apply, `memory.set` / `memory.forgotten` events (§4
   coverage note).
5. **Platform scopes** (#81) adopt the contract (§8).
6. **Pairing CLI** (#82) + seed procedure (§5, §7); stand up `roam` on the laptop.
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
- Pairing refuses (with a clear message) any participant pair with mismatched
  `contract_version` — a plugin pair OR the platform's own scope stream, which
  self-describes its contract at pairing time (§8/#95) and is refused on skew
  exactly like a plugin, rather than deferring the skew to a delivery-time
  `version_hold` — and warns on one-sided opt-in.
- Pairing survives a mixed-version rollout: a peer platform that predates the
  scope-stream self-manifest (§8) is discovered by falling back to the pre-#95
  synthesized participant (its contract_version left unknown, so the check
  defers to delivery-time, and a loud WARN names the remedy), so ONE
  un-upgraded peer never blocks pairing of the other participants.
- After pairing, BOTH directions verify: an event signed by either sender is
  accepted by its receiver (the handshake put the secret on the verifying
  side); rotating a stream's secret is hitless — old-signed deliveries are
  accepted during the switch and refused after retirement (§5).
- A one-sided SDK upgrade holds live streams instead of failing them:
  version-skew refusals are retryable in both directions, nothing
  dead-letters, and the held backlog drains once both sides run the new
  version (§3.2).
- `remember("x")` on both sides during a partition converges to the newer
  write on both sides after heal; a tombstoned `forget` beats an older
  `set`, and a newer `set` beats the tombstone (§4 memory note).
- Seeding per §7 loses nothing: a primary write authored between the stream
  priming and the dump, and another authored after the dump, each reach the
  spoke exactly once (snapshot or stream — never neither, never both applied
  twice). After first boot, both directions verify: the seed-injected
  inbound registration matches the primary's outbound secret, and the
  reverse direction pairs normally.
- Re-seeding under a fresh epoch (§3.2/§7) is fully accepted — no event of
  the new stream is rejected by the old epoch's watermark.
- An event whose apply keeps failing parks after the bound (§8.1): the park
  ACKs to the sender (its cursor advances past the parked seq), the stream
  resumes, the parked view shows it, and `applied_seq` stays pinned below
  the parked seq — even as later seqs apply past it. Re-applying it after
  the cause is fixed succeeds and unpins the frontier.
- After the §7 scrub, the spoke's first boot delivers nothing it didn't
  author: no cloned subscription, outbox row, watermark, or parked event
  survives the restore.
- Seeding per §7 yields a spoke that converges from events alone thereafter.
- A §7.1 union of two stores with unpaired history loses nothing: no write
  authored on either side during the partition is absent afterward; every
  cross-partition collision is LWW-resolved (same object) or flagged
  unreconciled (§6.1); a backfilled memory race resolves identically to its
  live equivalent (original authored-at as the LWW clock); shared-slug
  scopes converge with no park; a completed backfill re-run under a fresh
  epoch changes nothing; and after the backfill streams retire, steady
  state is BEHAVIORALLY indistinguishable from a §7-seeded pair — one live
  stream per direction, live causal context never contaminated by a
  backfill stream's frontier, and every live write (a `scope.updated` to a
  shared-slug scope included) replicates and applies cleanly even though
  scope UUIDs remain permanently instance-local.

## 11. Out of scope

- **Failover / shared endpoint / leader election** — the spoke is never
  promoted; deploy-continuity.md §7 stands. The agent on each machine points
  at that machine's gateway; endpoint choice is client config, not platform
  behavior.
- **fly.io / a third replica** — deferred with the revisit triggers in §2 (#83).
- **Trust changes** — the tailnet remains the boundary; OAuth is a separate
  seam (architecture.md §3.5).
- **General multi-master conflict resolution** (CRDTs, vector clocks) —
  rejected in §2; revisit only if a second *human* writer appears (#84).
- **Event-log retention / replay-from-genesis** — the bus stays a delta
  fabric; seeding is snapshot-based (§7).
