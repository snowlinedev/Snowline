"""Two-instance round-trip (replication-continuity §10, issue #77): a hub and a
spoke, each a real SDK replication store, wired sender→receiver through an
httpx transport that drives the receiver's `ingest_delivery` — no HTTP server.

Covers the §10 criteria that need BOTH halves live: a partitioned write lands
exactly once after "reconnect", redelivery is a watermark no-op, an applied
event NEVER re-emits (both outboxes go quiet — no echo), `peer_seen` rides the
reverse direction's envelopes, and secret rotation is hitless mid-backlog.
"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from snowline_plugin_sdk.replication import emit, ingest
from snowline_plugin_sdk.replication.models import ReplicationOutboxRow


class Instance:
    """One complete replication store + its domain side: an applied-events list
    standing in for the plugin's table, and an apply function that does what a
    real plugin's does — the idempotent domain write PLUS the emit hook (which
    origin suppression must silence)."""

    def __init__(self, make_instance, source_id: str):
        self.sessions = make_instance()
        self.source_id = source_id
        self.applied: list[dict] = []

    def apply(self, session, envelope: dict) -> None:
        # Idempotent by the domain payload's id (§4 checklist item 4).
        if any(e["payload"]["id"] == envelope["payload"]["id"] for e in self.applied):
            return
        self.applied.append(envelope)
        # The domain write's emit hook fires here — exactly the §3.2 boomerang
        # emit_event must suppress.
        emit.emit_event(session, envelope["event_type"], envelope["payload"])

    def write(self, payload: dict, event_type: str = "thing.recorded") -> None:
        """A locally-authored domain write: record + emit in one transaction."""
        with self.sessions() as s:
            self.applied.append({"event_type": event_type, "payload": payload})
            emit.emit_event(s, event_type, payload)
            s.commit()

    def outbox(self) -> list[ReplicationOutboxRow]:
        with self.sessions() as s:
            return list(s.scalars(select(ReplicationOutboxRow)))

    def ingest_transport(self) -> httpx.BaseTransport:
        """An httpx transport that plays this instance's ingest route: each
        POST runs `ingest_delivery` in its own session (the one-delivery-per-
        transaction contract), each GET answers 405 (reachable)."""
        instance = self

        class _T(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                if request.method == "GET":
                    return httpx.Response(405)
                session = instance.sessions()
                try:
                    status, body = ingest.ingest_delivery(
                        session,
                        request.content,
                        request.headers.get("X-Snowline-Signature"),
                        instance.apply,
                    )
                    session.commit()
                finally:
                    session.close()
                return httpx.Response(status, json=body)

        return _T()


def _pair(hub: Instance, spoke: Instance, monkeypatch) -> dict:
    """The §5 handshake, both directions: each receiver registers the inbound
    stream and MINTS the secret; each sender's subscription carries it. Returns
    the created subscription dicts by direction."""
    out = {}
    for sender, receiver, epoch in (
        (spoke, hub, "epoch-spoke-1"),
        (hub, spoke, "epoch-hub-1"),
    ):
        with receiver.sessions() as s:
            reg = ingest.register_inbound_stream(s, sender.source_id, epoch)
            s.commit()
        with sender.sessions() as s:
            monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", sender.source_id)
            out[sender.source_id] = emit.create_outbound_subscription(
                s,
                f"http://{receiver.source_id}/events/ingest",
                reg["secret"],
                ["thing.recorded"],
                epoch=epoch,
                peer_source_id=receiver.source_id,
            )
            s.commit()
    return out


def _deliver(sender: Instance, receiver: Instance) -> int:
    with httpx.Client(transport=receiver.ingest_transport()) as client:
        with sender.sessions() as s:
            n = emit.deliver_pending(s, client, reachability={})
            s.commit()
    return n


def test_partitioned_write_replicates_once_and_never_echoes(make_instance, monkeypatch):
    """§10: the spoke's offline write reaches the hub exactly once on
    'reconnect' (the outbox WAS the offline buffer), redelivery is a watermark
    no-op, and origin suppression keeps both outboxes quiet afterwards — no
    echo, even though the hub's apply runs the same emit hook a local write
    would."""
    hub = Instance(make_instance, "primary.plugin")
    spoke = Instance(make_instance, "roam.plugin")
    _pair(hub, spoke, monkeypatch)

    # Authored on the spoke while "partitioned" (nothing delivered yet).
    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "roam.plugin")
    spoke.write({"id": "d-1", "body": "decided offline"})
    assert [r.status for r in spoke.outbox()] == ["pending"]
    assert hub.applied == []

    # Reconnect: one delivery pass lands it.
    assert _deliver(spoke, hub) == 1
    assert [e["payload"]["id"] for e in hub.applied] == ["d-1"]

    # NO ECHO (§3.2 hard rule): the hub applied a spoke-authored write while
    # holding a live hub→spoke subscription — its outbox must stay empty.
    assert hub.outbox() == []
    assert [r.status for r in spoke.outbox()] == ["delivered"]

    # Redelivery is a no-op on the receiver's gate (§10): force a resend.
    with spoke.sessions() as s:
        row = s.scalars(select(ReplicationOutboxRow)).one()
        row.status = "pending"
        s.commit()
    assert _deliver(spoke, hub) == 1  # duplicate ACK still counts as delivered
    assert len(hub.applied) == 1  # applied exactly once


def test_peer_seen_rides_the_reverse_direction(make_instance, monkeypatch):
    """§3.2 causal context: after the hub applies the spoke's seqs 1..2, a
    hub-authored write carries peer_seen=2 back to the spoke — the §6.1
    concurrency input, stamped at emit from `applied_seq`."""
    hub = Instance(make_instance, "primary.plugin")
    spoke = Instance(make_instance, "roam.plugin")
    _pair(hub, spoke, monkeypatch)

    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "roam.plugin")
    spoke.write({"id": "d-1"})
    spoke.write({"id": "d-2"})
    assert _deliver(spoke, hub) == 2

    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "primary.plugin")
    hub.write({"id": "d-3"})
    (row,) = hub.outbox()
    assert row.payload["peer_seen"] == 2
    assert _deliver(hub, spoke) == 1
    assert spoke.applied[-1]["peer_seen"] == 2


def test_rotation_is_hitless_across_a_queued_backlog(make_instance, monkeypatch):
    """§5/§10: rotate the stream secret while the sender has a queued backlog.
    Old-signed deliveries are accepted during the switch; once the sender swaps
    (delivery-time signing re-signs the REMAINING backlog with the new secret),
    the first new-signed delivery retires the old — hitlessly, no event lost."""
    hub = Instance(make_instance, "primary.plugin")
    spoke = Instance(make_instance, "roam.plugin")
    subs = _pair(hub, spoke, monkeypatch)

    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "roam.plugin")
    for i in range(3):
        spoke.write({"id": f"d-{i}"})

    # Receiver mints the replacement mid-backlog…
    with hub.sessions() as s:
        rotated = ingest.rotate_inbound_secret(s, "roam.plugin", "epoch-spoke-1")
        s.commit()

    # …the sender hasn't swapped yet: the whole OLD-signed backlog still lands.
    assert _deliver(spoke, hub) == 3
    assert len(hub.applied) == 3

    # Sender swaps; the next write signs with the new secret and retires the old.
    with spoke.sessions() as s:
        emit.set_subscription_secret(s, subs["roam.plugin"]["id"], rotated["secret"])
        s.commit()
    spoke.write({"id": "d-3"})
    assert _deliver(spoke, hub) == 1
    with hub.sessions() as s:
        streams = {
            (st["source_id"], st["epoch"]): st for st in ingest.list_inbound_streams(s)
        }
    assert streams[("roam.plugin", "epoch-spoke-1")]["rotation_pending"] is False
    assert streams[("roam.plugin", "epoch-spoke-1")]["gate_seq"] == 4


def test_version_skew_holds_the_stream_without_dead_letter(make_instance, monkeypatch):
    """§10: a one-sided upgrade HOLDS the live stream — the receiver refuses
    retryably (409), the sender keeps the row pending under backoff, nothing
    dead-letters; the backlog drains once the versions align."""
    hub = Instance(make_instance, "primary.plugin")
    spoke = Instance(make_instance, "roam.plugin")
    _pair(hub, spoke, monkeypatch)

    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "roam.plugin")
    spoke.write({"id": "d-1"})

    # The spoke "upgraded first": its envelope claims a future version.
    with spoke.sessions() as s:
        row = s.scalars(select(ReplicationOutboxRow)).one()
        row.payload = {**row.payload, "contract_version": 3}
        s.commit()

    assert _deliver(spoke, hub) == 0
    with spoke.sessions() as s:
        row = s.scalars(select(ReplicationOutboxRow)).one()
        assert (row.status, row.attempts) == ("pending", 1)
        assert "version_hold" in row.last_error
        # The hub "finishes its upgrade" (the envelope speaks v2 again).
        row.payload = {**row.payload, "contract_version": 2}
        row.next_attempt_at = None
        s.commit()

    assert _deliver(spoke, hub) == 1  # the held backlog drains, nothing lost
    assert [e["payload"]["id"] for e in hub.applied] == ["d-1"]
