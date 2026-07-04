"""The INGEST half (replication-continuity §3.2/§5/§8.1, issue #77): the
per-stream delivery gate + contiguous apply, the gate/applied_seq split under
parking, version-skew holds, signature verify + hitless rotation, and the
origin-suppression flag around the plugin apply seam.

Everything drives `ingest_delivery` directly with hand-signed bodies — the
transport-agnostic seam the admin router shims onto (HTTP-level coverage lives
in test_replication_admin / the round-trip suite).
"""

from __future__ import annotations

import json

import pytest

from snowline_plugin_sdk.contract import CONTRACT_VERSION
from snowline_plugin_sdk.replication import ingest
from snowline_plugin_sdk.replication.envelope import build_envelope, sign_body
from snowline_plugin_sdk.replication.models import ReplicationInboundStream

STREAM = ("peer.plugin", "epoch-1")


class Applied:
    """A recording apply seam: collects envelopes; `fail_on` seqs raise (the
    §8.1 bounded-retryable path)."""

    def __init__(self, fail_on: set[int] | None = None):
        self.envelopes: list[dict] = []
        self.fail_on = fail_on or set()

    def __call__(self, session, envelope: dict) -> None:
        if envelope["seq"] in self.fail_on:
            raise RuntimeError(f"apply failed for seq {envelope['seq']}")
        self.envelopes.append(envelope)


def _register(session, **kwargs):
    out = ingest.register_inbound_stream(session, *STREAM, **kwargs)
    session.commit()  # registration precedes deliveries in its own transaction
    return out


def _delivery(secret: str, seq: int, *, payload: dict | None = None, **overrides):
    envelope = build_envelope(
        "thing.recorded",
        payload or {"n": seq},
        source_id=STREAM[0],
        epoch=STREAM[1],
        seq=seq,
        peer_seen=0,
    )
    envelope.update(overrides)
    body = json.dumps(envelope).encode()
    return body, f"sha256={sign_body(secret, body)}"


def _ingest(session, secret, seq, apply=None, **kw):
    """One delivery per transaction — the ingest TRANSACTION CONTRACT the admin
    route's per-request `session_scope` provides in production."""
    body, sig = _delivery(secret, seq)
    out = ingest.ingest_delivery(session, body, sig, apply or Applied(), **kw)
    session.commit()
    return out


def _stream(session):
    return session.get(ReplicationInboundStream, STREAM)


# --- the delivery gate: contiguous apply (§3.2) -------------------------------


def test_contiguous_apply_advances_both_counters(session):
    secret = _register(session)["secret"]
    apply = Applied()
    for seq in (1, 2, 3):
        status, body = _ingest(session, secret, seq, apply)
        assert (status, body["status"]) == (200, "applied")
    assert [e["seq"] for e in apply.envelopes] == [1, 2, 3]
    assert (_stream(session).gate_seq, _stream(session).applied_seq) == (3, 3)


def test_out_of_order_is_refused_with_expected_seq(session):
    """§3.2: the receiver applies exactly gate+1; a gap is a RETRYABLE refusal
    naming the expected seq — never applied out of order, never dead-lettered."""
    secret = _register(session)["secret"]
    apply = Applied()
    status, body = _ingest(session, secret, 3, apply)
    assert status == 409
    assert body == {"status": "refused", "reason": "out_of_order", "expected_seq": 1}
    assert apply.envelopes == []


def test_redelivery_at_or_under_the_gate_is_a_noop_ack(session):
    """At-least-once delivery: a duplicate ACKs 200 without re-applying."""
    secret = _register(session)["secret"]
    apply = Applied()
    _ingest(session, secret, 1, apply)
    status, body = _ingest(session, secret, 1, apply)
    assert (status, body["status"]) == (200, "duplicate")
    assert len(apply.envelopes) == 1  # applied exactly once


def test_apply_runs_under_origin_suppression(session):
    """§3.2 hard rule: the plugin's apply function (and any emit hook it calls)
    sees `is_applying_replicated_event()` True for exactly the apply's extent."""
    secret = _register(session)["secret"]
    seen: list[bool] = []

    def apply(s, envelope):
        seen.append(ingest.is_applying_replicated_event())

    _ingest(session, secret, 1, apply)
    assert seen == [True]
    assert ingest.is_applying_replicated_event() is False  # scoped, not sticky


def test_fresh_epoch_has_its_own_watermark(session):
    """§3.2/§7: a re-pair mints a fresh epoch whose stream starts at seq 1 —
    never rejected by the OLD epoch's watermark."""
    old_secret = _register(session)["secret"]
    _ingest(session, old_secret, 1)
    ingest.retire_inbound_stream(session, *STREAM)

    new = ingest.register_inbound_stream(session, STREAM[0], "epoch-2")
    envelope = build_envelope(
        "thing.recorded", {}, source_id=STREAM[0], epoch="epoch-2", seq=1, peer_seen=0
    )
    body = json.dumps(envelope).encode()
    status, resp = ingest.ingest_delivery(
        session, body, f"sha256={sign_body(new['secret'], body)}", Applied()
    )
    assert (status, resp["status"]) == (200, "applied")
    # …and the retired epoch now refuses as unknown (rejection class).
    status, resp = _ingest(session, old_secret, 2)
    assert (status, resp["reason"]) == (404, "unknown_stream")


# --- rejections vs holds (§3.1/§3.2) ------------------------------------------


def test_bad_signature_and_unknown_stream_are_rejections(session):
    secret = _register(session)["secret"]
    body, _ = _delivery(secret, 1)
    status, resp = ingest.ingest_delivery(session, body, "sha256=" + "0" * 64, Applied())
    assert (status, resp["reason"]) == (401, "bad_signature")

    other = build_envelope(
        "thing.recorded", {}, source_id="ghost.plugin", epoch="nope", seq=1, peer_seen=0
    )
    body = json.dumps(other).encode()
    status, resp = ingest.ingest_delivery(
        session, body, f"sha256={sign_body(secret, body)}", Applied()
    )
    assert (status, resp["reason"]) == (404, "unknown_stream")


def test_malformed_bodies_are_rejections(session):
    _register(session)
    status, resp = ingest.ingest_delivery(session, b"not json", "x", Applied())
    assert (status, resp["reason"]) == (400, "malformed_envelope")


def test_version_skew_holds_in_both_directions(session):
    """§3.2: a version-AHEAD envelope (peer upgraded first) AND a v1 envelope on
    a v2-paired stream are RETRYABLE refusals — never accept-and-misprocess,
    never dead-letter. Verified-signature first, so a forged 'v3' body can't
    probe the hold path."""
    secret = _register(session)["secret"]
    apply = Applied()

    # Version-AHEAD (v3 on our v2): hold.
    body, sig = _delivery(secret, 1, contract_version=CONTRACT_VERSION + 1)
    status, resp = ingest.ingest_delivery(session, body, sig, apply)
    assert (status, resp["reason"]) == (409, "version_hold")

    # Version-BEHIND (a v1 peer's envelope: no epoch/seq keying fields at all).
    v1_body = json.dumps(
        {"event_type": "thing.recorded", "source": STREAM[0], "contract_version": 1,
         "seq": 1, "payload": {}}
    ).encode()
    status, resp = ingest.ingest_delivery(
        session, v1_body, f"sha256={sign_body(secret, v1_body)}", apply
    )
    assert (status, resp["reason"]) == (409, "version_hold")

    assert apply.envelopes == []
    assert _stream(session).gate_seq == 0  # nothing gated through on a hold


# --- parking (§8.1): the gate/applied_seq split -------------------------------


def test_parking_after_the_bound_pins_the_applied_frontier(session):
    """The §10 parking criterion end-to-end: retryable failures 503 under the
    bound (sender redelivers), the bound parks LOUDLY — the park ACKs (200), the
    gate advances, the stream resumes — and `applied_seq` stays pinned below the
    parked seq even as later seqs apply past it. Re-applying after the fix
    unpins the frontier through the contiguously-applied span."""
    secret = _register(session)["secret"]
    apply = Applied(fail_on={2})
    _ingest(session, secret, 1, apply)

    # Two failures under the bound: retryable (503), counted, nothing parked.
    for attempt in (1, 2):
        status, resp = _ingest(session, secret, 2, apply, park_after=3)
        assert (status, resp["reason"]) == (503, "apply_failed")
        assert resp["attempts"] == attempt
    assert ingest.list_parked(session) == []
    assert (_stream(session).blocked_seq, _stream(session).blocked_attempts) == (2, 2)

    # The bound: parked, ACKed, gate past it, frontier pinned at 1.
    status, resp = _ingest(session, secret, 2, apply, park_after=3)
    assert (status, resp["status"]) == (200, "parked")
    stream = _stream(session)
    assert (stream.gate_seq, stream.applied_seq) == (2, 1)
    assert (stream.blocked_seq, stream.blocked_attempts) == (None, 0)

    parked = ingest.list_parked(session)
    assert [(p["seq"], p["event_type"]) for p in parked] == [(2, "thing.recorded")]
    assert "seq 2" in parked[0]["reason"]

    # The stream flows on; the frontier stays pinned (a max-style counter would
    # let the parked-unseen event masquerade as seen — the §3.2 trap).
    for seq in (3, 4):
        status, resp = _ingest(session, secret, seq, apply)
        assert (status, resp["status"]) == (200, "applied")
    assert (_stream(session).gate_seq, _stream(session).applied_seq) == (4, 1)

    # A redelivery of the PARKED seq is ACKed as a no-op (§8.1) — not re-applied.
    status, resp = _ingest(session, secret, 2, apply)
    assert (status, resp["status"]) == (200, "duplicate")

    # Fix the cause, re-apply from the park: applied, removed, frontier unpinned
    # THROUGH the contiguously-applied span beyond it (1 → 4 in one step).
    apply.fail_on = set()
    out = ingest.reapply_parked(session, *STREAM, 2, apply)
    assert ingest.list_parked(session) == []
    assert (out["gate_seq"], out["applied_seq"]) == (4, 4)
    assert [e["seq"] for e in apply.envelopes] == [1, 3, 4, 2]


def test_blocked_attempts_reset_when_a_different_seq_blocks(session):
    """The parking counter tracks ONE gate seq at a time — a later block starts
    its own count (the bound is per-event, not per-stream)."""
    secret = _register(session)["secret"]
    apply = Applied(fail_on={1, 2})
    _ingest(session, secret, 1, apply, park_after=5)
    assert _stream(session).blocked_attempts == 1
    # seq 1 heals and applies; seq 2 then blocks with a FRESH count.
    apply.fail_on = {2}
    _ingest(session, secret, 1, apply, park_after=5)
    status, resp = _ingest(session, secret, 2, apply, park_after=5)
    assert resp["attempts"] == 1
    assert _stream(session).blocked_seq == 2


def test_reapply_unknown_park_raises(session):
    _register(session)
    with pytest.raises(ValueError, match="no parked event"):
        ingest.reapply_parked(session, *STREAM, 9, Applied())


# --- rotation (§5): receiver mints, old+new during the switch -----------------


def test_rotation_is_hitless_and_retires_on_first_new_signed_delivery(session):
    secret_v1 = _register(session)["secret"]
    _ingest(session, secret_v1, 1)

    secret_v2 = ingest.rotate_inbound_secret(session, *STREAM)["secret"]
    assert secret_v2 != secret_v1
    assert _stream(session).previous_secret == secret_v1

    # During the switch: an OLD-signed delivery (the sender hasn't swapped yet)
    # is still accepted, and does NOT retire the old secret.
    status, resp = _ingest(session, secret_v1, 2)
    assert (status, resp["status"]) == (200, "applied")
    assert _stream(session).previous_secret == secret_v1

    # The FIRST new-signed delivery retires the old secret…
    status, resp = _ingest(session, secret_v2, 3)
    assert (status, resp["status"]) == (200, "applied")
    assert _stream(session).previous_secret is None

    # …after which old-signed deliveries are refused (§10's rotation criterion).
    status, resp = _ingest(session, secret_v1, 4)
    assert (status, resp["reason"]) == (401, "bad_signature")


def test_register_returns_secret_once_and_never_lists_it(session):
    reg = _register(session)
    assert reg["secret"]
    listed = ingest.list_inbound_streams(session)
    assert len(listed) == 1
    assert "secret" not in listed[0] and "previous_secret" not in listed[0]
    with pytest.raises(ValueError, match="already exists"):
        _register(session)
