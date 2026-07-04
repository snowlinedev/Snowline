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

# Full-write-surface coverage (replication-continuity §4 / §9 item 3, #79):
# one event type per lifecycle write, so two instances' governance stores can
# converge from events alone. The SHADOW graph events cover every shadow write
# (the conversation appender is ONE write path — `_append_conversation_event` —
# so message + agent.error share one event type); `shadow.graduated` is the
# provenance stamp both graduation shapes perform AFTER the decision event
# (node-level carries a `node_id`, branch-level carries none). The ARTIFACT
# events cover the spec/plan/reference docs — governance's "specs" ARE
# artifacts (`doc_kind`), there is no separate spec store.
EVENT_SHADOW_BRANCH_CREATED: str = "shadow.branch_created"
EVENT_SHADOW_BRANCH_ARCHIVED: str = "shadow.branch_archived"
EVENT_SHADOW_NOTES_SET: str = "shadow.notes_set"
EVENT_SHADOW_NODE_ADDED: str = "shadow.node_added"
EVENT_SHADOW_CITATION_ADDED: str = "shadow.citation_added"
EVENT_SHADOW_CONVERSATION_APPENDED: str = "shadow.conversation_appended"
EVENT_SHADOW_GRADUATED: str = "shadow.graduated"
EVENT_ARTIFACT_REGISTERED: str = "artifact.registered"
EVENT_ARTIFACT_REVISED: str = "artifact.revised"
EVENT_ARTIFACT_RESOLVED: str = "artifact.resolved"
EVENT_ARTIFACT_MATURITY_SET: str = "artifact.maturity_set"
EVENT_ARTIFACT_GOVERNS_SET: str = "artifact.governs_set"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_DECISION_RECORDED,
        EVENT_DECISION_SUPERSEDED,
        EVENT_SHADOW_BRANCH_CREATED,
        EVENT_SHADOW_BRANCH_ARCHIVED,
        EVENT_SHADOW_NOTES_SET,
        EVENT_SHADOW_NODE_ADDED,
        EVENT_SHADOW_CITATION_ADDED,
        EVENT_SHADOW_CONVERSATION_APPENDED,
        EVENT_SHADOW_GRADUATED,
        EVENT_ARTIFACT_REGISTERED,
        EVENT_ARTIFACT_REVISED,
        EVENT_ARTIFACT_RESOLVED,
        EVENT_ARTIFACT_MATURITY_SET,
        EVENT_ARTIFACT_GOVERNS_SET,
    }
)

# The published contract version, stamped into every emitted payload. A consumer
# (the SDK) refuses a payload whose `contract_version` is NEWER than it
# understands; bumping this is a deliberate major contract change.
# Version 2 (replication-continuity §3.2, #77): the stream envelope — `epoch`,
# EMIT-time `seq`, `peer_seen` — a breaking addition, bumped in BOTH pinned
# copies in one commit (the drift guard keeps them equal).
CONTRACT_VERSION: int = 2
