"""Vendored plugin-contract constants + the contract-version compatibility check
(the published contract dependency, issue #19).

These constants are a deliberate COPY of governance's PRODUCER copy
(`snowline_governance.contract`). The SDK is the published, light dependency that
EXTERNAL plugins install, so it must NOT depend on snowline-governance (or the
platform) at runtime — governance pulls sqlalchemy/psycopg, far too heavy for a
plugin client. Vendoring keeps this module to stdlib only; a drift-guard test
(`governance/tests/test_contract_drift.py`, a dev-only dep on this SDK) pins this
copy EQUAL to the governance source of truth so the two can never silently fork.

Pure constants + one pure function — no imports beyond stdlib.
"""

EVENT_DECISION_RECORDED: str = "decision.recorded"
EVENT_DECISION_SUPERSEDED: str = "decision.superseded"

# The platform's own adoption (replication-continuity §8, §9 item 5, issue
# #81): the scope namespace dogfoods the same contract it offers plugins.
EVENT_SCOPE_CREATED: str = "scope.created"
EVENT_SCOPE_UPDATED: str = "scope.updated"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_DECISION_RECORDED,
        EVENT_DECISION_SUPERSEDED,
        EVENT_SCOPE_CREATED,
        EVENT_SCOPE_UPDATED,
    }
)

# Version 2 (replication-continuity §3.2, #77): the stream envelope — `epoch`,
# EMIT-time `seq`, `peer_seen` — is a breaking addition over v1's
# delivery-time-seq shape. Without the bump, a v1 peer would silently accept
# and misprocess a v2 event under `check_contract_version`'s <= rule.
CONTRACT_VERSION: int = 2


class IncompatibleContractVersion(Exception):
    """Raised when an event/payload declares a `contract_version` NEWER than this
    SDK understands — i.e. the emitter (governance) is ahead of the consumer (this
    SDK). The consumer must upgrade before it can safely parse the payload."""


def check_contract_version(payload_version: int | None) -> None:
    """Raise `IncompatibleContractVersion` if an event/payload's contract version
    is newer (major-incompatible) than this SDK's `CONTRACT_VERSION`.

    Rule (kept deliberately simple for v1):
      * `None`  → a pre-versioning event; defaults to 1 and is ACCEPTED.
      * `<= CONTRACT_VERSION` → ACCEPTED (the SDK is at-or-ahead of the emitter).
      * `>  CONTRACT_VERSION` → REJECTED (the SDK is OLDER than the emitter and
        cannot assume forward compatibility across a major contract bump).

    The <= rule is for consumers of a STABLE envelope (fire-and-forget webhook
    subscribers via `verify_event`). Replication STREAMS do not use it: a
    stream's keying fields changed in v2, so `replication.ingest` holds ANY
    version-skewed envelope retryably in both directions
    (replication-continuity §3.2) rather than accepting an older one.
    """
    version = 1 if payload_version is None else payload_version
    if version > CONTRACT_VERSION:
        raise IncompatibleContractVersion(
            f"payload contract_version {version} is newer than this SDK's "
            f"CONTRACT_VERSION {CONTRACT_VERSION}; upgrade snowline-plugin-sdk"
        )
