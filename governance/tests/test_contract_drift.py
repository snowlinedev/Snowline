"""Producer↔consumer contract guard (governance-plugin spec §7).

Governance is the PRODUCER of the decision-event webhook contract; the published
`snowline-plugin-sdk` is the CONSUMER. Governance VENDORS its own copy of the
event-type registry + `CONTRACT_VERSION` (`snowline_governance.contract`) and must
NOT import the SDK at runtime (import-purity, spec §10). These DEV-ONLY tests pin
the vendored copy EQUAL to the SDK's so the two can never silently fork, and prove
an end-to-end round-trip: a payload governance signs is accepted by the consumer's
`verify_event`.

The SDK is a dev-only dependency; when it is not installed these tests skip (they
do not gate the import-pure suite).
"""

from __future__ import annotations

import json

import pytest

from snowline_governance import contract as gov_contract
from snowline_governance import replication

sdk_contract = pytest.importorskip("snowline_plugin_sdk.contract")
sdk_events = pytest.importorskip("snowline_plugin_sdk.events")


def test_contract_constants_equal_sdk():
    """The vendored producer constants EQUAL the SDK consumer's — the drift guard
    that keeps the wire contract byte-compatible across the two packages."""
    assert gov_contract.EVENT_DECISION_RECORDED == sdk_contract.EVENT_DECISION_RECORDED
    assert (
        gov_contract.EVENT_DECISION_SUPERSEDED
        == sdk_contract.EVENT_DECISION_SUPERSEDED
    )
    assert gov_contract.EVENT_TYPES == sdk_contract.EVENT_TYPES
    assert gov_contract.CONTRACT_VERSION == sdk_contract.CONTRACT_VERSION


class _StubDecision:
    """A minimal decision-row double for `build_decision_event` (no DB needed)."""

    def __init__(self):
        import uuid

        self.id = uuid.uuid4()
        self.scope_id = uuid.uuid4()
        self.decision = "use postgres"
        self.rationale = "it's solid"
        self.recorded_at = None
        self.supersedes_id = None


def test_producer_payload_roundtrips_through_sdk_verify_event():
    """The PRODUCER↔CONSUMER round-trip: governance builds + signs a payload
    EXACTLY as `deliver_pending` would, and the consumer's `verify_event` accepts
    the exact bytes under the shared secret (and rejects a tampered body / wrong
    secret)."""
    secret = "topsecret"
    payload = replication.build_decision_event(
        gov_contract.EVENT_DECISION_RECORDED, _StubDecision(), "acme/widget"
    )
    # Serialize EXACTLY as the delivery loop does (seq merged in, serialized once).
    body = json.dumps({**payload, "seq": 1}).encode()
    signature = f"sha256={replication.sign(secret, body)}"

    # The consumer accepts the exact bytes and returns the parsed event.
    out = sdk_events.verify_event(secret, body, signature)
    assert out["event_type"] == gov_contract.EVENT_DECISION_RECORDED
    assert out["contract_version"] == gov_contract.CONTRACT_VERSION
    assert out["seq"] == 1
    assert out["decision"]["decision"] == "use postgres"

    # A tampered body fails verification.
    with pytest.raises(sdk_events.BadSignature):
        sdk_events.verify_event(secret, body + b" ", signature)
    # The wrong secret fails verification.
    with pytest.raises(sdk_events.BadSignature):
        sdk_events.verify_event("wrong", body, signature)
