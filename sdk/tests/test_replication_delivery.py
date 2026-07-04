"""The §3.1 replication retry class (issue #77): unbounded retry with capped
per-row backoff, the per-ingest reachability probe + reconnect reset,
delivery-time signing over the exact bytes, per-stream contiguity at the
sender, and the rejection-vs-refusal split (ordering refusals carved OUT of the
dead-letter class).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
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
