"""The §3.1 replication retry class (issue #77): unbounded retry with capped
per-row backoff, the per-ingest reachability probe + reconnect reset,
delivery-time signing over the exact bytes, per-stream contiguity at the
sender, and the rejection-vs-refusal split (ordering refusals carved OUT of the
dead-letter class).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta

import anyio
import httpx
import pytest
from sqlalchemy import select

from snowline_plugin_sdk.replication import emit
from snowline_plugin_sdk.replication.envelope import sign_body
from snowline_plugin_sdk.replication.models import ReplicationOutboxRow

NOW = datetime(2026, 7, 4, 12, 0, 0)


class PeerTransport(httpx.BaseTransport):
    """A scriptable peer: `respond(request)` returns an `httpx.Response` or
    raises (transport error). Captures every request, probes (GET) included."""

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


def _ok(request: httpx.Request) -> httpx.Response:
    if request.method == "GET":
        return httpx.Response(405)  # a POST-only ingest answering = reachable
    return httpx.Response(200, json={"status": "applied"})


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("peer unreachable", request=request)


def _setup(session, *, url="http://peer.example/events/ingest", secret="s"):
    sub = emit.create_outbound_subscription(
        session, url, secret, ["thing.recorded"], epoch="e1"
    )
    return sub


def _rows(session):
    return list(
        session.scalars(
            select(ReplicationOutboxRow).order_by(ReplicationOutboxRow.seq)
        )
    )


def _deliver(session, transport, *, now=NOW, reachability=None):
    with httpx.Client(transport=transport) as client:
        return emit.deliver_pending(
            session, client, now=now,
            reachability=reachability if reachability is not None else {},
        )


def test_delivery_signs_the_exact_frozen_envelope_at_delivery_time(session):
    """Delivery-time signing over the exact bytes POSTed (§5's rotation
    contract): verify the HMAC by hand against the captured body, and the body
    IS the emit-time envelope (seq/peer_seen frozen at authoring)."""
    _setup(session, secret="topsecret")
    emit.emit_event(session, "thing.recorded", {"n": 1})
    emit.emit_event(session, "thing.recorded", {"n": 2})

    transport = PeerTransport(_ok)
    assert _deliver(session, transport) == 2

    posts = [r for r in transport.requests if r.method == "POST"]
    assert [json.loads(p.content)["seq"] for p in posts] == [1, 2]
    for post in posts:
        sig = post.headers["x-snowline-signature"]
        assert sig == f"sha256={sign_body('topsecret', post.content)}"
        assert json.loads(post.content)["contract_version"] == 2
    assert all(r.status == "delivered" for r in _rows(session))


def test_unreachability_never_dead_letters_and_backoff_caps(session):
    """§3.1: a two-week partition is normal operation. Attempts grow without
    any cap flipping the row terminal; `next_attempt_at` grows exponentially to
    the ceiling (~interval x 10) and no delivery fires before it's due."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {})

    down = PeerTransport(_down)
    now = NOW
    expected_gaps = [30, 60, 120, 240, 300, 300]  # capped at interval x 10
    for i, gap in enumerate(expected_gaps, start=1):
        row = _rows(session)[0]
        due = row.next_attempt_at or now
        now = max(now, due)
        assert _deliver(session, down, now=now) == 0
        row = _rows(session)[0]
        assert (row.status, row.attempts) == ("pending", i)  # NEVER dead-letters
        assert row.next_attempt_at == now + timedelta(seconds=gap)

    # Between ticks, a not-yet-due row is not attempted (and the probe alone
    # doesn't flush it — the peer is still down).
    posts_before = len([r for r in down.requests if r.method == "POST"])
    assert _deliver(session, down, now=now) == 0
    assert len([r for r in down.requests if r.method == "POST"]) == posts_before


def test_reconnect_reset_flushes_within_one_tick_of_the_heal(session):
    """§3.1's load-bearing probe: rows at the ceiling would not be due for
    minutes, but the unreachable→reachable transition resets their backoff and
    the SAME tick flushes them — §10's 'within one delivery interval of
    reconnect'."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {})
    reachability: dict[str, bool] = {}

    # Tick 1: peer down → backoff recorded, ingest marked unreachable.
    assert _deliver(session, PeerTransport(_down), reachability=reachability) == 0
    assert _rows(session)[0].next_attempt_at is not None
    assert reachability == {"http://peer.example/events/ingest": False}

    # Tick 2, five seconds later (row NOT due): the probe sees the heal, resets
    # the backoff, and the backlog flushes in this very tick.
    healed = PeerTransport(_ok)
    n = _deliver(
        session, healed, now=NOW + timedelta(seconds=5), reachability=reachability
    )
    assert n == 1
    assert [r.method for r in healed.requests] == ["GET", "POST"]  # probe, then flush
    assert _rows(session)[0].status == "delivered"


def test_ordering_refusals_and_version_holds_are_never_dead_lettered(session):
    """§3.1: a 409 (out_of_order / version_hold) is retryable BY DEFINITION —
    backoff, stay pending, never `rejected`."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {})

    def refuse(request):
        if request.method == "GET":
            return httpx.Response(405)
        return httpx.Response(
            409, json={"status": "refused", "reason": "out_of_order", "expected_seq": 9}
        )

    assert _deliver(session, PeerTransport(refuse)) == 0
    row = _rows(session)[0]
    assert (row.status, row.attempts) == ("pending", 1)
    assert "out_of_order" in row.last_error


def test_rejections_dead_letter_loudly(session):
    """§3.1: a delivered event the receiver REFUSED (bad signature class) is a
    bug, not a partition — terminal `rejected`, not retried on later ticks."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {})

    def reject(request):
        if request.method == "GET":
            return httpx.Response(405)
        return httpx.Response(401, json={"status": "rejected", "reason": "bad_signature"})

    transport = PeerTransport(reject)
    assert _deliver(session, transport) == 0
    row = _rows(session)[0]
    assert row.status == "rejected"
    assert "bad_signature" in row.last_error

    posts = len([r for r in transport.requests if r.method == "POST"])
    assert _deliver(session, transport) == 0  # not picked up again
    assert len([r for r in transport.requests if r.method == "POST"]) == posts


def test_a_failing_stream_blocks_only_itself(session):
    """§3.2/§10: the sender's cursor never advances past an undelivered seq —
    seq 2 is not attempted while seq 1 fails — and an unrelated stream on
    another peer keeps flowing. When the head finally succeeds the stream
    resumes with nothing skipped."""
    _setup(session, url="http://down.example/ingest")
    emit.create_outbound_subscription(
        session, "http://up.example/ingest", "s2", ["thing.recorded"], epoch="e2"
    )
    emit.emit_event(session, "thing.recorded", {"n": 1})
    emit.emit_event(session, "thing.recorded", {"n": 2})

    def split(request):
        if request.url.host == "down.example":
            if request.method == "GET":
                return httpx.Response(405)
            return httpx.Response(500)
        return _ok(request)

    transport = PeerTransport(split)
    assert _deliver(session, transport) == 2  # the healthy stream drained fully
    down_posts = [
        r for r in transport.requests
        if r.method == "POST" and r.url.host == "down.example"
    ]
    assert [json.loads(p.content)["seq"] for p in down_posts] == [1]  # 2 never sent

    # The head heals: the stream resumes IN ORDER, no seq skipped or re-sent
    # as already-seen (the receiver dedupes on its gate anyway).
    healed = PeerTransport(_ok)
    assert _deliver(session, healed, now=NOW + timedelta(seconds=31)) == 2
    healed_posts = [r for r in healed.requests if r.method == "POST"]
    assert [json.loads(p.content)["seq"] for p in healed_posts] == [1, 2]


def test_park_and_duplicate_acks_advance_the_cursor(session):
    """§8.1: a park ACKs exactly like a success, and a duplicate ACK is a
    no-op — both count as delivered, so a parked event never stalls its
    sender."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {"n": 1})
    emit.emit_event(session, "thing.recorded", {"n": 2})

    def park_then_dup(request):
        if request.method == "GET":
            return httpx.Response(405)
        seq = json.loads(request.content)["seq"]
        status = "parked" if seq == 1 else "duplicate"
        return httpx.Response(200, json={"status": status})

    assert _deliver(session, PeerTransport(park_then_dup)) == 2
    assert all(r.status == "delivered" for r in _rows(session))


@pytest.mark.parametrize(
    "respond_4xx",
    [
        lambda: httpx.Response(403),  # receiver trust gate (§5.1's config trap)
        lambda: httpx.Response(404, text="not found"),  # router not mounted yet
        lambda: httpx.Response(429, json={"detail": "slow down"}),  # proxy shape
        lambda: httpx.Response(400, json={"status": "refused"}),  # not the vocabulary
    ],
)
def test_stream_level_4xx_holds_the_backlog_instead_of_cascading(
    session, respond_4xx
):
    """The dead-letter gate: ONLY the ingest vocabulary's rejection body
    dead-letters. A bare/foreign-shaped 4xx (trust-gate 403, pre-mount 404,
    proxy 429…) is a stream-level condition — every row stays pending under
    backoff (the pre-fix behavior rejected a 3-row backlog in 3 ticks: a
    recoverable misconfiguration permanently destroying data), and the whole
    backlog delivers once the condition clears."""
    _setup(session)
    for i in range(3):
        emit.emit_event(session, "thing.recorded", {"n": i})

    def misconfigured(request):
        if request.method == "GET":
            return httpx.Response(405)
        return respond_4xx()

    # Three ticks (each past the head's backoff): nothing rejects, ever.
    now = NOW
    for _ in range(3):
        row = _rows(session)[0]
        now = max(now, row.next_attempt_at or now)
        assert _deliver(session, PeerTransport(misconfigured), now=now) == 0
        assert [r.status for r in _rows(session)] == ["pending"] * 3

    # The misconfiguration clears: the full backlog delivers in order.
    healed = PeerTransport(_ok)
    now = max(now, _rows(session)[0].next_attempt_at or now)
    assert _deliver(session, healed, now=now) == 3
    posts = [r for r in healed.requests if r.method == "POST"]
    assert [json.loads(p.content)["seq"] for p in posts] == [1, 2, 3]
    assert all(r.status == "delivered" for r in _rows(session))


def test_requeue_rejected_resumes_the_wedged_stream(session):
    """A true vocabulary rejection wedges its stream (later seqs draw
    out-of-order refusals from the receiver's gate, never dead-letters);
    `list_rejected` surfaces it and `requeue_rejected` puts it back at the
    head, letting the whole backlog resume."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {"n": 1})
    emit.emit_event(session, "thing.recorded", {"n": 2})

    def reject_seq_1(request):
        if request.method == "GET":
            return httpx.Response(405)
        seq = json.loads(request.content)["seq"]
        if seq == 1:
            return httpx.Response(
                401, json={"status": "rejected", "reason": "bad_signature"}
            )
        return httpx.Response(
            409, json={"status": "refused", "reason": "out_of_order", "expected_seq": 1}
        )

    # Tick 1: seq 1 dead-letters; seq 2 is not attempted this tick.
    assert _deliver(session, PeerTransport(reject_seq_1)) == 0
    assert [r.status for r in _rows(session)] == ["rejected", "pending"]

    # Tick 2: seq 2 becomes the head and draws an ordering REFUSAL — the wedge
    # is loud but nothing else dead-letters.
    assert _deliver(session, PeerTransport(reject_seq_1)) == 0
    assert [r.status for r in _rows(session)] == ["rejected", "pending"]

    rejected = emit.list_rejected(session)
    assert [(r["seq"], r["source_id"], r["epoch"]) for r in rejected] == [
        (1, "test.plugin", "e1")
    ]
    assert "bad_signature" in rejected[0]["last_error"]

    # Fix the cause, requeue the head: the stream resumes in order.
    emit.requeue_rejected(session, rejected[0]["id"])
    assert emit.list_rejected(session) == []
    healed = PeerTransport(_ok)
    now = NOW + timedelta(seconds=600)  # past seq 2's backoff
    assert _deliver(session, healed, now=now) == 2
    posts = [r for r in healed.requests if r.method == "POST"]
    assert [json.loads(p.content)["seq"] for p in posts] == [1, 2]


# --- the `enabled` defer/gate seam (issue #91) --------------------------------
#
# `replication_delivery_loop` used to fire its first tick immediately with no
# built-in way to keep a freshly booted app quiet, so the platform, memory, and
# governance each hand-rolled (or, for governance, leaned on the blunter
# process-wide env var) their own app-level on/off switch. These tests pin the
# seam itself: disabled does zero DB/network activity, the default stays
# byte-for-byte the pre-#91 "tick immediately" behavior, and the pre-existing
# `SNOWLINE_REPLICATION_DISABLED` escape hatch still works alongside it.


def test_enabled_false_never_touches_session_scope_or_network():
    """The seam's whole point: `enabled=False` must be a pure no-op — it must
    return without ever calling `session_scope` (so a disabled loop on a test
    boot can never open a session, let alone hit the network)."""

    def session_scope():
        raise AssertionError(
            "session_scope must never be called while enabled=False"
        )

    async def main():
        # If the loop failed to return early this would hang rather than
        # fail cleanly, so bound it.
        with anyio.fail_after(2):
            await emit.replication_delivery_loop(session_scope, enabled=False)

    anyio.run(main)


def test_enabled_true_default_still_ticks_immediately(session, monkeypatch):
    """The default (`enabled` omitted, matching every existing bare
    `tg.start_soon(replication_delivery_loop, session_scope)` call) must keep
    behaving exactly as it did before the seam existed: the first tick fires
    immediately, not after waiting out the delivery interval."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {})
    transport = PeerTransport(_ok)

    class _TestClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    @contextmanager
    def session_scope():
        yield session

    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(emit.replication_delivery_loop, session_scope)
            # A huge interval: if the first tick waited for it instead of
            # firing immediately, this test would time out below rather than
            # false-pass.
            with anyio.fail_after(2):
                while not any(r.method == "POST" for r in transport.requests):
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

    monkeypatch.setattr(httpx, "Client", _TestClient)
    monkeypatch.setenv("SNOWLINE_REPLICATION_INTERVAL", "999")
    anyio.run(main)

    assert _rows(session)[0].status == "delivered"


def test_replication_disabled_env_var_still_works_alongside_enabled(
    session, monkeypatch
):
    """The pre-#91 escape hatch (`SNOWLINE_REPLICATION_DISABLED`, still used by
    e.g. governance's autouse test fixture) keeps disabling the loop even when
    the caller leaves the new `enabled` parameter at its default `True` — the
    two gates are additive (either one disables), not a replacement."""
    monkeypatch.setenv("SNOWLINE_REPLICATION_DISABLED", "1")

    def session_scope():
        raise AssertionError(
            "session_scope must never be called while the env var is set"
        )

    async def main():
        with anyio.fail_after(2):
            await emit.replication_delivery_loop(session_scope)

    anyio.run(main)


def _reject_with(reason: str, status: int = 400):
    def _respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(405)
        return httpx.Response(status, json={"status": "rejected", "reason": reason})

    return _respond


# --- issue #108: the retired-subscription requeue guard -----------------------


def test_requeue_onto_retired_subscription_is_invisible_limbo_without_the_guard(
    session,
):
    """The bug this guard exists to kill, reproduced directly (write this
    FIRST, per the issue): flipping a rejected row back to `pending` under a
    RETIRED subscription — exactly what `requeue_rejected` did before the
    guard existed — produces UNDELIVERABLE limbo. `deliver_pending` only ever
    queries ACTIVE subscriptions, so the row is invisible to it: not parked,
    not rejected, never attempted, never delivered."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {"n": 1})
    assert _deliver(session, PeerTransport(_reject_with("bad_signature", 401))) == 0

    row = _rows(session)[0]
    assert row.status == "rejected"
    emit.retire_outbound_subscription(session, str(row.subscription_id))

    # The pre-guard behavior: flip the row back to pending directly, bypassing
    # any subscription check (what `requeue_rejected` unconditionally did).
    row.status = "pending"
    row.attempts = 0
    row.next_attempt_at = None
    session.commit()

    healed = PeerTransport(_ok)
    assert _deliver(session, healed) == 0  # never even attempted
    assert healed.requests == []
    assert _rows(session)[0].status == "pending"  # invisible limbo, confirmed


def test_requeue_rejected_refuses_a_retired_subscription(session):
    """The guard (issue #108): `requeue_rejected` refuses — instead of
    creating the limbo above — when the row's subscription is retired, and
    the row stays untouched (`rejected`, never flipped to `pending`)."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {"n": 1})
    assert _deliver(session, PeerTransport(_reject_with("bad_signature", 401))) == 0

    rejected = emit.list_rejected(session)
    row_id, sub_id = rejected[0]["id"], rejected[0]["subscription_id"]
    emit.retire_outbound_subscription(session, sub_id)

    with pytest.raises(emit.RequeueRefusedError) as exc_info:
        emit.requeue_rejected(session, row_id)
    detail = exc_info.value.detail
    assert detail["reason"] == "subscription_retired"
    assert detail["subscription_id"] == sub_id
    assert "successor_subscription_id" not in detail  # no re-pair happened

    assert [r["id"] for r in emit.list_rejected(session)] == [row_id]  # untouched


def test_requeue_rejected_names_the_successor_after_a_rotation(session):
    """Rotation is retain-not-refuse (ee5794e): re-pairing with the same peer
    retires the old subscription and mints a fresh-epoch successor, which
    stays discoverable. The refusal names it, pointing the operator at the
    live stream instead of the dead one."""
    sub = _setup(session)
    emit.emit_event(session, "thing.recorded", {"n": 1})
    assert _deliver(session, PeerTransport(_reject_with("bad_signature", 401))) == 0

    row_id = emit.list_rejected(session)[0]["id"]
    emit.retire_outbound_subscription(session, sub["id"])
    successor = emit.create_outbound_subscription(
        session, sub["target_url"], "new-secret", ["thing.recorded"], epoch="e2"
    )

    with pytest.raises(emit.RequeueRefusedError) as exc_info:
        emit.requeue_rejected(session, row_id)
    detail = exc_info.value.detail
    assert detail["successor_subscription_id"] == successor["id"]
    assert detail["successor_epoch"] == "e2"


# --- issue #107: bulk requeue-by-stream ----------------------------------------


def test_requeue_rejected_bulk_resumes_a_vocabulary_cascade(session):
    """The canonical mass-rejection shape (#107): a producer ships a new event
    type before this consumer upgrades, so every row of that type
    dead-letters until the fix lands. One bulk call requeues the whole
    cascade and returns the count requeued."""
    _setup(session)
    for n in range(3):
        emit.emit_event(session, "thing.recorded", {"n": n})

    reject_all = PeerTransport(_reject_with("malformed_envelope"))
    # Each tick dead-letters exactly the stream head (it wedges behind it);
    # three ticks are needed to reject all three queued rows.
    for _ in range(3):
        _deliver(session, reject_all)
    assert [r.status for r in _rows(session)] == ["rejected"] * 3

    sub_id = emit.list_rejected(session)[0]["subscription_id"]
    result = emit.requeue_rejected_bulk(session, sub_id)
    assert result == {
        "subscription_id": sub_id,
        "source_id": "test.plugin",
        "epoch": "e1",
        "requeued": 3,
    }
    assert [r.status for r in _rows(session)] == ["pending"] * 3
    assert emit.list_rejected(session) == []


def test_requeue_rejected_bulk_filters_by_event_type_and_reason(session):
    """Filters are respected: only the rows matching `event_type` (and/or
    `reason`) requeue; the rest stay rejected for a later, separate call."""
    with_types = emit.create_outbound_subscription(
        session,
        "http://peer.example/events/ingest",
        "s2",
        ["thing.recorded", "other.thing"],
        epoch="e2",
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(405)
        event_type = json.loads(request.content)["event_type"]
        if event_type == "thing.recorded":
            return httpx.Response(
                400, json={"status": "rejected", "reason": "malformed_envelope"}
            )
        return httpx.Response(
            401, json={"status": "rejected", "reason": "bad_signature"}
        )

    emit.emit_event(session, "thing.recorded", {"n": 1})  # seq1 on sub "e2"
    emit.emit_event(session, "other.thing", {"n": 2})  # seq2 on sub "e2"
    emit.emit_event(session, "thing.recorded", {"n": 3})  # seq3 on sub "e2"

    transport = PeerTransport(respond)
    for _ in range(3):
        _deliver(session, transport)
    assert [r.status for r in _rows(session)] == ["rejected"] * 3

    # Narrow by event_type: only the two thing.recorded rows requeue.
    result = emit.requeue_rejected_bulk(
        session, with_types["id"], event_type="thing.recorded"
    )
    assert result["requeued"] == 2
    remaining = emit.list_rejected(session)
    assert [(r["event_type"], r["reason"]) for r in remaining] == [
        ("other.thing", "bad_signature")
    ]

    # Narrow the leftover by reason: the last row requeues too.
    result = emit.requeue_rejected_bulk(
        session, with_types["id"], reason="bad_signature"
    )
    assert result["requeued"] == 1
    assert emit.list_rejected(session) == []


def test_requeue_rejected_bulk_refuses_whole_call_on_retired_subscription(session):
    """The bulk decision for issue #108: a retired target refuses the WHOLE
    call (nothing requeued) rather than skip-and-report a partial count —
    consistent with the per-row guard's refuse-loudly posture."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {"n": 1})
    assert _deliver(session, PeerTransport(_reject_with("malformed_envelope"))) == 0

    sub_id = emit.list_rejected(session)[0]["subscription_id"]
    emit.retire_outbound_subscription(session, sub_id)

    with pytest.raises(emit.RequeueRefusedError) as exc_info:
        emit.requeue_rejected_bulk(session, sub_id)
    assert exc_info.value.detail["reason"] == "subscription_retired"
    assert len(emit.list_rejected(session)) == 1  # untouched


def test_requeue_rejected_bulk_unknown_subscription(session):
    with pytest.raises(ValueError, match="no replication subscription"):
        emit.requeue_rejected_bulk(session, "00000000-0000-0000-0000-000000000000")


def test_requeue_rejected_bulk_rerun_is_idempotent(session):
    """A second bulk call over an already-requeued set is a clean
    `requeued: 0` — nothing left in `rejected` matches, nothing double-flips,
    no error (rerunning a recovery script is safe)."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {"n": 1})
    assert _deliver(session, PeerTransport(_reject_with("malformed_envelope"))) == 0

    sub_id = emit.list_rejected(session)[0]["subscription_id"]
    assert emit.requeue_rejected_bulk(session, sub_id)["requeued"] == 1
    assert emit.requeue_rejected_bulk(session, sub_id)["requeued"] == 0
    assert [r.status for r in _rows(session)] == ["pending"]


def test_requeue_rejected_bulk_combined_event_type_and_reason_filters(session):
    """Both filters in ONE call intersect: only the row matching the
    event_type AND the reason requeues; every other combination stays."""
    emit.create_outbound_subscription(
        session,
        "http://peer.example/events/ingest",
        "s2",
        ["thing.recorded", "other.thing"],
        epoch="e2",
    )

    reasons = {1: "malformed_envelope", 2: "bad_signature", 3: "malformed_envelope"}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(405)
        seq = json.loads(request.content)["seq"]
        return httpx.Response(
            400, json={"status": "rejected", "reason": reasons[seq]}
        )

    emit.emit_event(session, "thing.recorded", {"n": 1})  # seq1: malformed
    emit.emit_event(session, "thing.recorded", {"n": 2})  # seq2: bad_signature
    emit.emit_event(session, "other.thing", {"n": 3})  # seq3: malformed

    transport = PeerTransport(respond)
    for _ in range(3):
        _deliver(session, transport)
    assert [r.status for r in _rows(session)] == ["rejected"] * 3

    sub_id = emit.list_rejected(session)[0]["subscription_id"]
    result = emit.requeue_rejected_bulk(
        session, sub_id, event_type="thing.recorded", reason="malformed_envelope"
    )
    assert result["requeued"] == 1  # only seq 1 matches both
    remaining = emit.list_rejected(session)
    assert [(r["seq"], r["event_type"], r["reason"]) for r in remaining] == [
        (2, "thing.recorded", "bad_signature"),
        (3, "other.thing", "malformed_envelope"),
    ]


def test_requeue_rejected_bulk_rejects_a_typo_reason_loudly(session):
    """`reason` is a CLOSED vocabulary (only REJECTION_REASONS values can
    appear on a rejected row), so a typo'd filter raises instead of answering
    a silent `requeued: 0` that reads as 'already handled'."""
    sub = _setup(session)
    with pytest.raises(ValueError, match="unknown rejection reason"):
        emit.requeue_rejected_bulk(session, sub["id"], reason="bad_signatur")


def test_requeue_ambiguous_successor_is_not_named(session):
    """TWO active subscriptions to the same peer target: the refusal still
    fires but names NO successor — pointing at either would be a guess, and
    a wrong pointer is worse than none."""
    sub = _setup(session)
    emit.emit_event(session, "thing.recorded", {"n": 1})
    assert _deliver(session, PeerTransport(_reject_with("bad_signature", 401))) == 0

    row_id = emit.list_rejected(session)[0]["id"]
    emit.retire_outbound_subscription(session, sub["id"])
    for epoch in ("e2", "e3"):
        emit.create_outbound_subscription(
            session, sub["target_url"], f"s-{epoch}", ["thing.recorded"], epoch=epoch
        )

    with pytest.raises(emit.RequeueRefusedError) as exc_info:
        emit.requeue_rejected(session, row_id)
    detail = exc_info.value.detail
    assert detail["reason"] == "subscription_retired"
    assert "successor_subscription_id" not in detail
    assert "successor_epoch" not in detail


def test_requeue_guard_loads_the_subscription_with_for_update(session, monkeypatch):
    """Pins the guard's QUERY SHAPE: both requeue paths must load the
    subscription `with_for_update=True` so the retired check serializes
    against a concurrent retire under Postgres READ COMMITTED (a plain read
    could pass the guard, the retire commit, and the flip land on a
    now-retired stream — the #108 limbo through the guarded path). SQLite
    ignores FOR UPDATE, so the race itself can't run here; the lock flag on
    the load is the testable contract."""
    from sqlalchemy.orm import Session as _Session

    from snowline_plugin_sdk.replication.models import ReplicationSubscription

    _setup(session)
    emit.emit_event(session, "thing.recorded", {"n": 1})
    assert _deliver(session, PeerTransport(_reject_with("bad_signature", 401))) == 0
    rejected = emit.list_rejected(session)
    row_id, sub_id = rejected[0]["id"], rejected[0]["subscription_id"]

    lock_flags = []
    orig_get = _Session.get

    def spying_get(self, entity, ident, **kw):
        if entity is ReplicationSubscription:
            lock_flags.append(kw.get("with_for_update"))
        return orig_get(self, entity, ident, **kw)

    monkeypatch.setattr(_Session, "get", spying_get)
    emit.requeue_rejected(session, row_id)
    emit.requeue_rejected_bulk(session, sub_id)  # requeued: 0 — flags still load
    assert lock_flags == [True, True]
