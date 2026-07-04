"""Plugin-contract event registry — the published contract governance EMITs
(governance-plugin spec §7).

These constants are a deliberate, self-contained COPY of the monolith's
`snowline_substrate.contract` (#663) / the `snowline-plugin-sdk`'s vendored
`snowline_plugin_sdk.contract` (#666, decision 3fa71698). Governance is the
PRODUCER; the SDK is the CONSUMER. Both vendor the SAME literals so the wire
contract is byte-compatible, while neither imports the other at runtime:

  - Governance must NOT import the SDK at runtime — it is an independent plugin
    (import-purity: `snowline_governance` pulls no monolith/substrate code, and
    the SDK is a separate dev-only round-trip dependency, spec §10).
  - The SDK must NOT import substrate (it is the light dependency external
    plugins install).

So the registry is VENDORED in three places and pinned EQUAL by drift-guard
tests. Governance's guard (`tests/test_contract_drift.py`, a dev-only dep on the
SDK) asserts THIS copy equals `snowline_plugin_sdk.contract`'s, so the producer
and consumer can never silently fork.

Pure constants — no imports beyond stdlib.
"""

from __future__ import annotations

EVENT_DECISION_RECORDED: str = "decision.recorded"
EVENT_DECISION_SUPERSEDED: str = "decision.superseded"

# Memory's replication vocabulary (replication-continuity §4 coverage note, #80).
# Governance does not EMIT these — but EVENT_TYPES is the whole platform's
# drift-guarded vocabulary, not just governance's own: §3.2 pins every plugin's
# event types into BOTH copies (this producer copy and the SDK's) in one commit,
# so the drift guard (`tests/test_contract_drift.py`) keeps them byte-equal.
EVENT_MEMORY_SET: str = "memory.set"
EVENT_MEMORY_FORGOTTEN: str = "memory.forgotten"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_DECISION_RECORDED,
        EVENT_DECISION_SUPERSEDED,
        EVENT_MEMORY_SET,
        EVENT_MEMORY_FORGOTTEN,
    }
)

# The published contract version, stamped into every emitted payload. A consumer
# (the SDK) refuses a payload whose `contract_version` is NEWER than it
# understands; bumping this is a deliberate major contract change.
# Version 2 (replication-continuity §3.2, #77): the stream envelope — `epoch`,
# EMIT-time `seq`, `peer_seen` — a breaking addition, bumped in BOTH pinned
# copies in one commit (the drift guard keeps them equal).
# DEPLOY ORDERING: this version rides the legacy bus's payloads too
# (`build_decision_event` stamps it), and an SDK-v1 consumer's `verify_event`
# rejects it — the bus's attempt cap (default 5) then dead-letters those
# deliveries. Upgrade webhook consumers before or together with governance.
CONTRACT_VERSION: int = 2
