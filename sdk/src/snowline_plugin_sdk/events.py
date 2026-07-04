"""The EVENT half of the plugin contract — verify + parse signed webhook
deliveries (the published contract dependency, issue #19).

Governance's EMIT bus (`snowline_governance.replication`) POSTs each decision
event as `json.dumps({**payload, "seq": seq}).encode()` with an
`X-Snowline-Signature: sha256=<hex>` header, where
`<hex> = hmac.new(secret.encode(), body, sha256).hexdigest()` (see
`replication.sign`). This module is the consumer's counterpart: HMAC-verify the
raw bytes, then parse + version-check.

Subscription REGISTRATION has NO remote surface for the fire-and-forget webhook
class — those subscriptions are created programmatically server-side
(`replication.create_subscription`, no MCP/CLI surface); registering such a
subscriber is an operator / out-of-band step, and this module only verifies and
parses the deliveries that result. SUPERSEDED for REPLICATION-CLASS
subscriptions (replication-continuity spec §5, #77): the SDK's
`replication.admin` module ships a tailnet-gated replication-admin HTTP surface
next to `ingest_path` (create/list/retire inbound registrations + outbound
subscriptions, the receiver-mints-secret handshake, rotation) that the pairing
CLI drives — still OFF MCP; agents never manage plumbing.
"""

import hashlib
import hmac
import json

from .contract import (
    EVENT_DECISION_RECORDED,
    EVENT_DECISION_SUPERSEDED,
    EVENT_TYPES,
    IncompatibleContractVersion,
    check_contract_version,
)

__all__ = [
    "BadSignature",
    "IncompatibleContractVersion",
    "verify_event",
    "EVENT_DECISION_RECORDED",
    "EVENT_DECISION_SUPERSEDED",
    "EVENT_TYPES",
]


class BadSignature(Exception):
    """Raised when a webhook delivery's HMAC signature does not match the body
    under the shared secret (tampered body, wrong secret, or malformed header)."""


def verify_event(secret: str, body: bytes, signature: str) -> dict:
    """Verify a webhook delivery's HMAC and parse it.

    `body` is the EXACT raw POST bytes (verifying a re-serialization would break,
    since the signature is computed over the precise bytes the emitter sent).
    `signature` is the `X-Snowline-Signature` header value — either the
    `sha256=<hex>` form the emitter sends or a bare `<hex>`.

    Recomputes `hmac.new(secret.encode(), body, sha256).hexdigest()` (mirroring
    governance's `replication.sign` byte-for-byte) and compares constant-time
    (`hmac.compare_digest`). On mismatch raises `BadSignature`. On success,
    `json.loads(body)`, runs `check_contract_version(payload.get(
    "contract_version"))`, and returns the parsed event dict.
    """
    provided = signature[len("sha256=") :] if signature.startswith("sha256=") else signature
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise BadSignature("HMAC signature does not match request body")

    payload = json.loads(body)
    check_contract_version(payload.get("contract_version"))
    return payload
