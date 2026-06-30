"""SDK-own unit tests for `verify_event` — standalone (no governance dependency).

These pin the consumer's signature-verification + version-check behavior. The
round-trip is proven against a LOCAL `sign()` that mirrors governance's
`replication.sign` byte-for-byte (HMAC-SHA256 hexdigest over the exact bytes);
the producer↔consumer cross-package guard lives in governance's
`test_contract_drift.py`.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from snowline_plugin_sdk import contract
from snowline_plugin_sdk.events import (
    BadSignature,
    IncompatibleContractVersion,
    verify_event,
)


def _sign(secret: str, body: bytes) -> str:
    """Mirror of governance's `replication.sign` — HMAC-SHA256 hexdigest."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _signed(secret: str, payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    return body, f"sha256={_sign(secret, body)}"


def _payload(**overrides) -> dict:
    base = {
        "event_type": contract.EVENT_DECISION_RECORDED,
        "source": "governance",
        "contract_version": contract.CONTRACT_VERSION,
        "decision": {"id": "d1", "decision": "use postgres"},
        "seq": 1,
    }
    base.update(overrides)
    return base


def test_valid_round_trip_returns_parsed_event():
    secret = "topsecret"
    body, sig = _signed(secret, _payload())
    out = verify_event(secret, body, sig)
    assert out["event_type"] == contract.EVENT_DECISION_RECORDED
    assert out["contract_version"] == contract.CONTRACT_VERSION
    assert out["seq"] == 1
    assert out["decision"]["decision"] == "use postgres"


def test_bare_hex_signature_is_accepted():
    """The header may arrive as a bare hex digest, without the `sha256=` prefix."""
    secret = "topsecret"
    body = json.dumps(_payload()).encode()
    out = verify_event(secret, body, _sign(secret, body))
    assert out["seq"] == 1


def test_tampered_body_raises_bad_signature():
    secret = "topsecret"
    body, sig = _signed(secret, _payload())
    with pytest.raises(BadSignature):
        verify_event(secret, body + b" ", sig)


def test_wrong_secret_raises_bad_signature():
    body, sig = _signed("topsecret", _payload())
    with pytest.raises(BadSignature):
        verify_event("wrong", body, sig)


def test_newer_contract_version_is_rejected():
    secret = "topsecret"
    newer = contract.CONTRACT_VERSION + 1
    body, sig = _signed(secret, _payload(contract_version=newer))
    with pytest.raises(IncompatibleContractVersion):
        verify_event(secret, body, sig)


def test_missing_contract_version_defaults_accepted():
    """A pre-versioning payload (no `contract_version`) defaults to 1, ACCEPTED."""
    secret = "topsecret"
    payload = _payload()
    payload.pop("contract_version")
    body, sig = _signed(secret, payload)
    out = verify_event(secret, body, sig)
    assert "contract_version" not in out
