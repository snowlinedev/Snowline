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
    """A recording apply seam: collects envelopes; `fail_on` seqs raise a
    bounded-retryable error (the §8.1 retry-then-park path), `park_now_on` seqs
    raise `ParkNow` (the #92 permanent-failure fast path — immediate park)."""

    def __init__(
        self,
        fail_on: set[int] | None = None,
        park_now_on: set[int] | None = None,
    ):
        self.envelopes: list[dict] = []
        self.fail_on = fail_on or set()
        self.park_now_on = park_now_on or set()

    def __call__(self, session, envelope: dict) -> None:
        seq = envelope["seq"]
        if seq in self.park_now_on:
            raise ingest.ParkNow(f"permanent failure for seq {seq}")
        if seq in self.fail_on:
            raise RuntimeError(f"apply failed for seq {seq}")
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


def test_park_now_parks_immediately_on_the_first_delivery(session):
    """The #92 fast path: an apply that raises `ParkNow` parks on the FIRST
    delivery attempt — no 503 retries, no budget spent — yet lands the SAME
    §8.1 first-class state as a bound-reached park (park ACK, gate advance,
    `applied_seq` pin, re-appliable). Here seq 1 parks immediately, so the
    frontier pins at 0 (nothing applied before it)."""
    secret = _register(session)["secret"]
    apply = Applied(park_now_on={1})

    status, resp = _ingest(session, secret, 1, apply)
    assert (status, resp["status"]) == (200, "parked")
    stream = _stream(session)
    # The gate advanced past the park; the frontier pinned below it; no retry
    # counting happened (blocked_* untouched at their reset value).
    assert (stream.gate_seq, stream.applied_seq) == (1, 0)
    assert (stream.blocked_seq, stream.blocked_attempts) == (None, 0)

    parked = ingest.list_parked(session)
    assert [(p["seq"], p["event_type"]) for p in parked] == [(1, "thing.recorded")]
    assert "permanent failure for seq 1" in parked[0]["reason"]
    assert apply.envelopes == []  # the apply never recorded — it was parked

    # A redelivery of the parked seq is a no-op ACK (§8.1), not a re-apply.
    status, resp = _ingest(session, secret, 1, apply)
    assert (status, resp["status"]) == (200, "duplicate")

    # The stream flows on past the park.
    status, resp = _ingest(session, secret, 2, apply)
    assert (status, resp["status"]) == (200, "applied")
    assert (_stream(session).gate_seq, _stream(session).applied_seq) == (2, 0)

    # Fix the cause, re-apply from the park: the frontier unpins through the
    # contiguously-applied span (0 → 2 in one step).
    apply.park_now_on = set()
    out = ingest.reapply_parked(session, *STREAM, 1, apply)
    assert ingest.list_parked(session) == []
    assert (out["gate_seq"], out["applied_seq"]) == (2, 2)


def _run_scenario_to_park(session, source_id, epoch, *, park_now: bool):
    """Drive one stream through the identical shape — seq 1 applied, seq 2 parked,
    seqs 3+4 applied past the park — parking seq 2 either by reaching the bound
    (`park_now=False`) or by `ParkNow` (`park_now=True`). Returns the parked-row
    dict and the stream watermark for a field-by-field parity comparison."""
    reg = ingest.register_inbound_stream(session, source_id, epoch)
    secret = reg["secret"]
    session.commit()

    def deliver(seq, apply):
        env = build_envelope(
            "thing.recorded", {"n": seq},
            source_id=source_id, epoch=epoch, seq=seq, peer_seen=0,
        )
        body = json.dumps(env).encode()
        out = ingest.ingest_delivery(
            session, body, f"sha256={sign_body(secret, body)}", apply, park_after=3
        )
        session.commit()
        return out

    if park_now:
        apply = Applied(park_now_on={2})
        deliver(1, apply)
        status, resp = deliver(2, apply)  # parks on the first attempt
    else:
        apply = Applied(fail_on={2})
        deliver(1, apply)
        for _ in range(2):  # two 503s under the bound of 3…
            deliver(2, apply)
        status, resp = deliver(2, apply)  # …the third reaches the bound and parks
    assert (status, resp["status"]) == (200, "parked")
    for seq in (3, 4):
        deliver(seq, apply)

    stream = session.get(ReplicationInboundStream, (source_id, epoch))
    parked = [p for p in ingest.list_parked(session) if p["source_id"] == source_id]
    return parked, stream


def test_park_now_state_is_bit_for_bit_a_bound_reached_park(session):
    """The critical #92 invariant: a `ParkNow` park must surface state IDENTICAL
    to a bound-reached park. Drive two streams through the same shape — one
    reaching the bound, one via `ParkNow` — and assert the parked row and the
    stream watermark match field for field. The ONLY sanctioned divergences are
    the `reason` string (each records its own `str(exc)`) and `parked_at` (a
    timestamp); everything the sender and the §8.1 surface observe is identical."""
    bound_parked, bound_stream = _run_scenario_to_park(
        session, "peer.bound", "epoch-b", park_now=False
    )
    now_parked, now_stream = _run_scenario_to_park(
        session, "peer.now", "epoch-n", park_now=True
    )

    # The stream watermark + parking counters land identically.
    assert (bound_stream.gate_seq, bound_stream.applied_seq) == (4, 1)
    assert (now_stream.gate_seq, now_stream.applied_seq) == (4, 1)
    assert (bound_stream.gate_seq, bound_stream.applied_seq) == (
        now_stream.gate_seq, now_stream.applied_seq
    )
    assert (bound_stream.blocked_seq, bound_stream.blocked_attempts) == (None, 0)
    assert (now_stream.blocked_seq, now_stream.blocked_attempts) == (None, 0)

    # The parked row is identical field-for-field. `reason`/`parked_at` differ
    # by design (own str(exc) / timestamp); `source_id`/`epoch` and the
    # payload's echoed `source`/`epoch` differ only because the two scenarios
    # ran on two distinct streams — normalise those stream-keying fields out.
    assert len(bound_parked) == 1 and len(now_parked) == 1

    def _normalise(row):
        r = {k: v for k, v in row.items() if k not in {"reason", "parked_at", "source_id", "epoch"}}
        r["payload"] = {k: v for k, v in r["payload"].items() if k not in {"source", "epoch"}}
        return r

    b = _normalise(bound_parked[0])
    n = _normalise(now_parked[0])
    assert b == n
    assert b["seq"] == 2 and b["event_type"] == "thing.recorded"
    assert b["payload"]["seq"] == 2  # the WHOLE envelope was parked, not just the body


def test_park_now_mixes_with_transient_retries_on_a_live_stream(session):
    """The mixed case (#92): a transient apply error retries under the bound and
    then heals, while a later `ParkNow` event parks immediately — the two paths
    coexist on one stream. seq 2's transient failures 503 then apply; seq 4
    parks on its first delivery; the frontier pins at 3."""
    secret = _register(session)["secret"]
    apply = Applied(fail_on={2}, park_now_on={4})

    # seq 1 applies clean.
    status, resp = _ingest(session, secret, 1, apply)
    assert (status, resp["status"]) == (200, "applied")

    # seq 2 fails transiently twice (503, counted), then heals and applies.
    for attempt in (1, 2):
        status, resp = _ingest(session, secret, 2, apply, park_after=5)
        assert (status, resp["reason"], resp["attempts"]) == (503, "apply_failed", attempt)
    assert ingest.list_parked(session) == []
    apply.fail_on = set()
    status, resp = _ingest(session, secret, 2, apply, park_after=5)
    assert (status, resp["status"]) == (200, "applied")

    # seq 3 applies; seq 4 raises ParkNow and parks on its FIRST attempt.
    status, resp = _ingest(session, secret, 3, apply)
    assert (status, resp["status"]) == (200, "applied")
    status, resp = _ingest(session, secret, 4, apply, park_after=5)
    assert (status, resp["status"]) == (200, "parked")

    stream = _stream(session)
    assert (stream.gate_seq, stream.applied_seq) == (4, 3)  # frontier pins below the park
    assert (stream.blocked_seq, stream.blocked_attempts) == (None, 0)
    parked = ingest.list_parked(session)
    assert [(p["seq"], p["reason"]) for p in parked] == [(4, "permanent failure for seq 4")]
    assert [e["seq"] for e in apply.envelopes] == [1, 2, 3]  # 4 never applied


def test_park_now_mid_backoff_resets_the_counting_state(session):
    """`_park_now`'s promised reset: a seq that accrued transient 503s and THEN
    raises `ParkNow` on a later redelivery parks immediately — the committed
    `blocked_seq`/`blocked_attempts` land at `(None, 0)`, the same reset a
    bound-reached park performs, and the park state matches (gate advanced,
    frontier pinned, row parked)."""
    secret = _register(session)["secret"]
    apply = Applied(fail_on={1})

    # Two transient failures on seq 1: counting state committed at (1, 2).
    for attempt in (1, 2):
        status, resp = _ingest(session, secret, 1, apply, park_after=5)
        assert (status, resp["reason"], resp["attempts"]) == (503, "apply_failed", attempt)
    assert (_stream(session).blocked_seq, _stream(session).blocked_attempts) == (1, 2)

    # The SAME seq now turns out permanent (e.g. deeper inspection on a later
    # attempt): ParkNow parks it well under the bound of 5.
    apply.fail_on = set()
    apply.park_now_on = {1}
    status, resp = _ingest(session, secret, 1, apply, park_after=5)
    assert (status, resp["status"]) == (200, "parked")

    stream = _stream(session)
    assert (stream.blocked_seq, stream.blocked_attempts) == (None, 0)
    assert (stream.gate_seq, stream.applied_seq) == (1, 0)  # gate past, frontier pinned
    parked = ingest.list_parked(session)
    assert [(p["seq"], p["reason"]) for p in parked] == [(1, "permanent failure for seq 1")]


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


def test_double_rotation_keeps_the_sender_verified_secret(session):
    """The double-rotate race: the pairing CLI crashes between mint and carry,
    the operator re-runs the rotation. The re-rotate must KEEP the secret the
    sender provably still signs with (s1) and discard only the never-delivered
    mint (s2) — overwriting previous_secret with s2 would strand the sender at
    s1 → 401s."""
    s1 = _register(session)["secret"]
    _ingest(session, s1, 1)

    s2 = ingest.rotate_inbound_secret(session, *STREAM)["secret"]
    s3 = ingest.rotate_inbound_secret(session, *STREAM)["secret"]
    session.commit()
    assert len({s1, s2, s3}) == 3
    assert _stream(session).previous_secret == s1  # NOT s2

    # The sender (still on s1) keeps flowing through both rotations…
    status, resp = _ingest(session, s1, 2)
    assert (status, resp["status"]) == (200, "applied")
    # …the lost mint never verifies…
    status, resp = _ingest(session, s2, 3)
    assert (status, resp["reason"]) == (401, "bad_signature")
    # …and the carried replacement completes the rotation normally.
    status, resp = _ingest(session, s3, 3)
    assert (status, resp["status"]) == (200, "applied")
    assert _stream(session).previous_secret is None  # s1 retired
    status, resp = _ingest(session, s1, 4)
    assert (status, resp["reason"]) == (401, "bad_signature")


def test_peer_seen_is_validated_like_seq(session):
    """`peer_seen` flows raw into the apply seam and §6.1's concurrency
    detection consumes it — a non-int / negative value is a 400 rejection, not
    silently applied."""
    secret = _register(session)["secret"]
    for bad in ("garbage", -1, None, True):
        body, sig = _delivery(secret, 1, peer_seen=bad)
        status, resp = ingest.ingest_delivery(session, body, sig, Applied())
        assert (status, resp["reason"]) == (400, "malformed_envelope"), bad
    # …and the stream is untouched: the valid seq 1 still applies.
    status, resp = _ingest(session, secret, 1)
    assert (status, resp["status"]) == (200, "applied")
