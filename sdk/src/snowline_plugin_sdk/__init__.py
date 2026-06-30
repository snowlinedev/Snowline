"""Snowline plugin SDK — the published, versioned contract a plugin pins to
consume the platform's governance core (issue #19).

Two halves were specified in the frozen monolith:
  * REQUEST/RESPONSE — a typed async MCP client (`SnowlineCore`) over the core's
    MCP tool surface. This is DEFERRED here as a follow-up: the platform's
    surfaces differ from the monolith's `/core`, and the typed client pulls the
    heavyweight `mcp` dependency. It is intentionally NOT shipped in this package
    yet (see #19 / the package pyproject note).
  * EVENT — `verify_event`, which HMAC-verifies + parses governance's signed
    decision webhooks, version-checked against the vendored `CONTRACT_VERSION`.
    This half is shipped here and is the contract the drift-guard test pins.
"""

from .contract import (
    CONTRACT_VERSION,
    EVENT_DECISION_RECORDED,
    EVENT_DECISION_SUPERSEDED,
    EVENT_TYPES,
    IncompatibleContractVersion,
    check_contract_version,
)
from .events import BadSignature, verify_event

__all__ = [
    "verify_event",
    "BadSignature",
    "IncompatibleContractVersion",
    "check_contract_version",
    "CONTRACT_VERSION",
    "EVENT_DECISION_RECORDED",
    "EVENT_DECISION_SUPERSEDED",
    "EVENT_TYPES",
]
