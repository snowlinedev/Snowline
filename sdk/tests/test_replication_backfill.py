"""The §7.1 backfill stream machinery (replication-continuity §7.1, issue #98):
the reserved `<source_id>#backfill` identity, the direct-enqueue walker (empty
vocabulary, `peer_seen` 0, deterministic enqueue-time seq), the backfill
lifecycle (open / status / retire), the parked-event DROP verb, and the admin
routes that drive them — on the SDK's standalone in-memory stores.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import anyio
import httpx
import pytest
from fastapi import FastAPI
from snowline_plugin_sdk.replication import emit, ingest
from snowline_plugin_sdk.replication.admin import build_replication_router
from snowline_plugin_sdk.replication.envelope import (
    backfill_source_id,
    build_envelope,
    is_backfill_envelope,
    is_backfill_source,
    live_source_id,
    sign_body,
)
from snowline_plugin_sdk.replication.models import (
    ReplicationOutboxRow,
    ReplicationSubscription,
)
from sqlalchemy import select

LIVE = "mini.thing"
BACKFILL = "mini.thing#backfill"
TARGET = "http://roam/events/ingest"


def _rows(n: int):
    """A deterministic toy enumerator over `n` domain rows."""

    def enumerate_rows(session):
        for i in range(1, n + 1):
            yield ("thing.recorded", {"id": f"row-{i}", "at": f"2026-07-0{i}T00:00:00"})

    return enumerate_rows


def _outbox(session) -> list[ReplicationOutboxRow]:
    return list(session.scalars(select(ReplicationOutboxRow).order_by(ReplicationOutboxRow.seq)))


# --- identity -----------------------------------------------------------------


def test_backfill_identity_helpers():
    assert backfill_source_id(LIVE) == BACKFILL
    assert backfill_source_id(BACKFILL) == BACKFILL  # idempotent
    assert is_backfill_source(BACKFILL) and not is_backfill_source(LIVE)
    assert not is_backfill_source(None)
    assert live_source_id(BACKFILL) == LIVE
    assert live_source_id(LIVE) == LIVE
    assert is_backfill_envelope({"source": BACKFILL})
    assert not is_backfill_envelope({"source": LIVE})


# --- the walker ----------------------------------------------------------------


def test_open_walks_enumerator_into_outbox_with_backfill_semantics(session):
    out = emit.open_backfill_subscription(
        session, TARGET, "s3cret", epoch="bf1", live_source_id=LIVE, enumerator=_rows(3)
    )
    session.commit()

    assert out["source_id"] == BACKFILL
    assert out["event_types"] == []  # EMPTY vocabulary — invisible to emit
    assert out["peer_source_id"] is None
    assert out["final_seq"] == 3
    assert out["enumerated"] == {"thing.recorded": 3}

    rows = _outbox(session)
    assert [r.seq for r in rows] == [1, 2, 3]
    # Deterministic enumeration order, peer_seen 0, no extra marker field.
    assert [r.payload["payload"]["id"] for r in rows] == ["row-1", "row-2", "row-3"]
    for r in rows:
        env = r.payload
        assert env["source"] == BACKFILL and env["epoch"] == "bf1"
        assert env["peer_seen"] == 0
        assert set(env) == {
            "event_type", "contract_version", "source", "epoch", "seq",
            "peer_seen", "payload",
        }
        # Original authored-at rides through untouched (the §6 LWW clock).
        assert env["payload"]["at"].startswith("2026-07-0")


def test_empty_side_enumerates_nothing(session):
    out = emit.open_backfill_subscription(
        session, TARGET, "s", epoch="bf1", live_source_id=LIVE, enumerator=_rows(0)
    )
    assert out["final_seq"] == 0
    assert _outbox(session) == []


def test_live_writes_never_fan_onto_a_backfill_stream(session):
    """A live write emitted DURING an open backfill lands only on the live
    stream — even a (hand-built) backfill subscription declaring the live
    vocabulary is skipped by identity."""
    emit.create_outbound_subscription(
        session, TARGET, "live", ["thing.recorded"], epoch="e1", source_id=LIVE
    )
    emit.open_backfill_subscription(
        session, TARGET, "s", epoch="bf1", live_source_id=LIVE, enumerator=_rows(2)
    )
    # Belt and braces: give the backfill row the live vocabulary by hand.
    bf = session.scalars(
        select(ReplicationSubscription).where(ReplicationSubscription.source_id == BACKFILL)
    ).one()
    bf.event_types = ["thing.recorded"]
    session.flush()

    envelopes = emit.emit_event(session, "thing.recorded", {"id": "live-1"})
    assert [e["source"] for e in envelopes] == [LIVE]
    bf_rows = [r for r in _outbox(session) if r.subscription_id == bf.id]
    assert [r.seq for r in bf_rows] == [1, 2]  # the walker's counter untouched


def test_one_active_backfill_per_target(session):
    first = emit.open_backfill_subscription(
        session, TARGET, "s", epoch="bf1", live_source_id=LIVE, enumerator=_rows(1)
    )
    with pytest.raises(ValueError, match="already active"):
        emit.open_backfill_subscription(
            session, TARGET, "s", epoch="bf2", live_source_id=LIVE, enumerator=_rows(1)
        )
    emit.retire_backfill_subscription(session, first["id"])
    again = emit.open_backfill_subscription(
        session, TARGET, "s", epoch="bf2", live_source_id=LIVE, enumerator=_rows(1)
    )
    assert again["final_seq"] == 1  # a fresh epoch restarts at seq 1


def test_retire_backfill_refuses_a_live_stream(session):
    live = emit.create_outbound_subscription(
        session, TARGET, "live", ["thing.recorded"], epoch="e1", source_id=LIVE
    )
    with pytest.raises(ValueError, match="live stream"):
        emit.retire_backfill_subscription(session, live["id"])


def test_receiver_keeps_live_and_backfill_streams_side_by_side(session):
    """The `#backfill` suffix keeps the §5 single-active-stream invariant intact:
    the live and backfill streams coexist (different sources), while the SAME
    invariant gives one backfill per direction at a time."""
    ingest.register_inbound_stream(session, LIVE, "e1")
    ingest.register_inbound_stream(session, BACKFILL, "bf1")
    with pytest.raises(ValueError, match="already active"):
        ingest.register_inbound_stream(session, BACKFILL, "bf2")
    ingest.retire_inbound_stream(session, BACKFILL, "bf1")
    ingest.register_inbound_stream(session, BACKFILL, "bf2")


# --- round trip through the ordinary delivery loop -------------------------------


class _Receiver:
    def __init__(self, make_instance):
        self.sessions = make_instance()
        self.applied: list[dict] = []

    def apply(self, session, envelope):
        if any(e["payload"]["id"] == envelope["payload"]["id"] for e in self.applied):
            return  # idempotent by domain id
        self.applied.append(envelope)

    def transport(self) -> httpx.BaseTransport:
        rec = self

        class _T(httpx.BaseTransport):
            def handle_request(self, request):
                if request.method == "GET":
                    return httpx.Response(405)
                s = rec.sessions()
                try:
                    status, body = ingest.ingest_delivery(
                        s, request.content, request.headers.get("X-Snowline-Signature"),
                        rec.apply,
                    )
                    s.commit()
                finally:
                    s.close()
                return httpx.Response(status, json=body)

        return _T()


def test_backfill_drains_through_the_ordinary_delivery_loop(make_instance):
    sender = make_instance()
    receiver = _Receiver(make_instance)
    with receiver.sessions() as s:
        secret = ingest.register_inbound_stream(s, BACKFILL, "bf1")["secret"]
        s.commit()
    with sender() as s:
        emit.open_backfill_subscription(
            s, TARGET, secret, epoch="bf1", live_source_id=LIVE, enumerator=_rows(4)
        )
        s.commit()

    with httpx.Client(transport=receiver.transport()) as client, sender() as s:
        assert emit.deliver_pending(s, client, reachability={}) == 4
        status = emit.backfill_status(s)

    assert [e["payload"]["id"] for e in receiver.applied] == [
        "row-1", "row-2", "row-3", "row-4",
    ]
    assert all(e["peer_seen"] == 0 for e in receiver.applied)
    (bf,) = status
    assert (bf["final_seq"], bf["delivered"], bf["pending"], bf["head_seq"]) == (4, 4, 0, None)
    with receiver.sessions() as s:
        (stream,) = ingest.list_inbound_streams(s)
    assert stream["applied_seq"] == bf["final_seq"]  # the union's completion check


# --- the parked-event DROP verb ------------------------------------------------------


def _park_one(session):
    secret = ingest.register_inbound_stream(session, LIVE, "e1")["secret"]
    session.commit()

    def park(session, envelope):
        raise ingest.ParkNow("permanent")

    def ok(session, envelope):
        return None

    for seq, fn in ((1, park), (2, ok)):
        env = build_envelope("thing.recorded", {"id": f"x{seq}"}, source_id=LIVE,
                             epoch="e1", seq=seq, peer_seen=0)
        body = json.dumps(env).encode()
        ingest.ingest_delivery(session, body, f"sha256={sign_body(secret, body)}", fn)
        session.commit()


def test_drop_parked_removes_the_park_and_unpins_the_frontier(session):
    _park_one(session)
    (stream,) = ingest.list_inbound_streams(session)
    assert (stream["gate_seq"], stream["applied_seq"]) == (2, 0)  # seq 1 pins it

    out = ingest.drop_parked(session, LIVE, "e1", 1)
    assert out["dropped"]["seq"] == 1 and out["dropped"]["reason"] == "permanent"
    assert out["stream"]["applied_seq"] == 2  # unpinned by declaration
    assert ingest.list_parked(session) == []
    with pytest.raises(ValueError, match="no parked event"):
        ingest.drop_parked(session, LIVE, "e1", 1)


def test_drop_parked_tolerates_a_vanished_stream_row(session):
    _park_one(session)
    ingest.retire_inbound_stream(session, LIVE, "e1")
    out = ingest.drop_parked(session, LIVE, "e1", 1)
    assert out["stream"]["active"] is False


# --- the admin routes ------------------------------------------------------------------


def _app(make_instance, *, enumerator=None, apply=None):
    sessions = make_instance()

    @contextmanager
    def scope():
        s = sessions()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = FastAPI()
    app.include_router(
        build_replication_router(
            scope, apply or (lambda s, e: None), enumerator=enumerator
        )
    )
    return app, sessions


def _call(app, method, path, **kwargs):
    async def main():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 4242))
        async with httpx.AsyncClient(transport=transport, base_url="http://plugin") as c:
            return await c.request(method, path, **kwargs)

    return anyio.run(main)


def test_admin_backfill_open_without_enumerator_is_501(make_instance):
    app, _ = _app(make_instance)
    resp = _call(app, "POST", "/replication-admin/backfill/open", json={
        "target_url": TARGET, "secret": "s", "epoch": "bf1", "source_id": LIVE,
    })
    assert resp.status_code == 501


def test_admin_backfill_lifecycle(make_instance):
    app, _ = _app(make_instance, enumerator=_rows(2))
    body = {"target_url": TARGET, "secret": "s", "epoch": "bf1", "source_id": LIVE}
    opened = _call(app, "POST", "/replication-admin/backfill/open", json=body)
    assert opened.status_code == 200, opened.text
    assert opened.json()["final_seq"] == 2
    assert opened.json()["source_id"] == BACKFILL
    # A second open toward the same target refuses while the first is active.
    again = _call(app, "POST", "/replication-admin/backfill/open",
                  json={**body, "epoch": "bf2"})
    assert again.status_code == 409

    listed = _call(app, "GET", "/replication-admin/backfill").json()
    assert [(b["epoch"], b["pending"], b["final_seq"]) for b in listed] == [("bf1", 2, 2)]

    retired = _call(app, "POST", "/replication-admin/backfill/retire",
                    json={"id": opened.json()["id"]})
    assert retired.status_code == 200 and retired.json()["active"] is False
    # The live outbound list still shows it (retired streams stay for audit).
    outs = _call(app, "GET", "/replication-admin/outbound").json()
    assert [o["active"] for o in outs] == [False]


def test_admin_parked_drop_and_reapply(make_instance):
    applied: list[dict] = []
    app, sessions = _app(make_instance, apply=lambda s, e: applied.append(e))
    with sessions() as s:
        _park_one(s)
    # Re-apply seq 1 through the admin route (the route's apply succeeds).
    resp = _call(app, "POST", "/replication-admin/parked/reapply",
                 json={"source_id": LIVE, "epoch": "e1", "seq": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied_seq"] == 2
    assert [e["payload"]["id"] for e in applied] == ["x1"]
    # Nothing left to drop.
    resp = _call(app, "POST", "/replication-admin/parked/drop",
                 json={"source_id": LIVE, "epoch": "e1", "seq": 1})
    assert resp.status_code == 404


def test_admin_parked_drop(make_instance):
    app, sessions = _app(make_instance)
    with sessions() as s:
        _park_one(s)
    resp = _call(app, "POST", "/replication-admin/parked/drop",
                 json={"source_id": LIVE, "epoch": "e1", "seq": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json()["stream"]["applied_seq"] == 2
    assert _call(app, "GET", "/replication-admin/parked").json() == []
