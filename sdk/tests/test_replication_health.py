"""The delivery-health block a plugin's /health carries (issue #241): outbox
shape, per-peer failure runs set/cleared by delivery outcomes, tick-error
capture and the >= 3 rule, the unreachable threshold (+ env override), and the
ok/degraded verdict."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta

import anyio
import httpx
import pytest

from snowline_plugin_sdk.replication import emit
from snowline_plugin_sdk.replication.health import (
    DELIVERY_HEALTH,
    DeliveryHealth,
    apply_to_health,
    replication_health,
    replication_health_from_scope,
)

NOW = datetime(2026, 7, 4, 12, 0, 0)
URL = "http://peer.example/events/ingest"


class PeerTransport(httpx.BaseTransport):
    def __init__(self, respond):
        self.respond = respond

    def handle_request(self, request):
        return self.respond(request)


def _ok(request):
    if request.method == "GET":
        return httpx.Response(405)
    return httpx.Response(200, json={"status": "applied"})


def _down(request):
    raise httpx.ConnectError("Connection refused", request=request)


def _deliver(session, respond, now, state):
    with httpx.Client(transport=PeerTransport(respond)) as client:
        return emit.deliver_pending(
            session, client, now=now, reachability={}, health=state
        )


def _setup(session, peer_source_id="hub.pm"):
    emit.create_outbound_subscription(
        session, URL, "s", ["thing.recorded"], epoch="e1",
        peer_source_id=peer_source_id,
    )
    session.commit()


def test_outbox_block_shape_with_no_subscriptions(session):
    block = replication_health(session, now=NOW, state=DeliveryHealth())
    assert block == {
        "status": "ok",
        "degraded_reason": None,
        "outbox": {
            "pending": 0,
            "oldest_pending_age_s": None,
            "peers": {},
            "tick": {"last_at": None, "last_error": None, "consecutive_errors": 0},
        },
    }


def test_pending_counts_and_oldest_age_per_peer(session):
    _setup(session)
    emit.emit_event(session, "thing.recorded", {"n": 1})
    emit.emit_event(session, "thing.recorded", {"n": 2})
    session.commit()
    from sqlalchemy import select

    from snowline_plugin_sdk.replication.models import ReplicationOutboxRow

    rows = list(session.scalars(select(ReplicationOutboxRow)))
    for r in rows:
        r.created_at = NOW - timedelta(seconds=120)
    session.commit()

    block = replication_health(session, now=NOW, state=DeliveryHealth())
    assert block["status"] == "ok"
    assert block["outbox"]["pending"] == 2
    assert block["outbox"]["oldest_pending_age_s"] == 120.0
    assert block["outbox"]["peers"] == {
        "hub.pm": {
            "pending": 2,
            "consecutive_failures": 0,
            "last_success_at": None,
            "last_error": None,
            "unreachable_since": None,
        }
    }


def test_peer_keyed_by_url_when_unpaired_in_reverse(session):
    _setup(session, peer_source_id=None)
    block = replication_health(session, now=NOW, state=DeliveryHealth())
    assert list(block["outbox"]["peers"]) == [URL]


def test_failures_set_unreachable_since_and_success_clears_it(session):
    state = DeliveryHealth()
    _setup(session)
    emit.emit_event(session, "thing.recorded", {})
    session.commit()

    _deliver(session, _down, NOW, state)
    later = NOW + timedelta(minutes=5)
    _deliver(session, _down, later, state)
    peer = replication_health(session, now=later, state=state)["outbox"]["peers"]["hub.pm"]
    assert peer["consecutive_failures"] == 2
    assert peer["unreachable_since"] == NOW.isoformat() + "Z"  # first failure of the run
    assert peer["last_error"] == "ConnectError: Connection refused"
    assert peer["pending"] == 1

    healed = later + timedelta(minutes=5)
    assert _deliver(session, _ok, healed, state) == 1
    block = replication_health(session, now=healed, state=state)
    peer = block["outbox"]["peers"]["hub.pm"]
    assert peer["consecutive_failures"] == 0
    assert peer["unreachable_since"] is None
    assert peer["last_success_at"] == healed.isoformat() + "Z"
    assert peer["pending"] == 0
    assert block["status"] == "ok"


def test_http_failures_count_too(session):
    state = DeliveryHealth()
    _setup(session)
    emit.emit_event(session, "thing.recorded", {})
    session.commit()
    _deliver(session, lambda r: httpx.Response(503), NOW, state)
    peer = replication_health(session, now=NOW, state=state)["outbox"]["peers"]["hub.pm"]
    assert peer["consecutive_failures"] == 1
    assert peer["last_error"].startswith("HTTP 503")


def test_consecutive_failures_survive_restart_via_head_attempts(session):
    """In-memory state is per-process; the head row's attempts keep a long
    wedge's failure count visible after a restart."""
    _setup(session)
    emit.emit_event(session, "thing.recorded", {})
    session.commit()
    _deliver(session, _down, NOW, DeliveryHealth())
    _deliver(session, _down, NOW + timedelta(hours=1), DeliveryHealth())
    peer = replication_health(session, now=NOW, state=DeliveryHealth())["outbox"]["peers"]["hub.pm"]
    assert peer["consecutive_failures"] == 2
    # After a restart it is seeded from the stuck row (#269), not unknown.
    assert peer["unreachable_since"] is not None


def test_unreachable_past_threshold_degrades(session):
    state = DeliveryHealth()
    _setup(session)
    emit.emit_event(session, "thing.recorded", {})
    session.commit()
    _deliver(session, _down, NOW, state)

    at_14m = replication_health(session, now=NOW + timedelta(minutes=14), state=state)
    assert at_14m["status"] == "ok"

    at_16m = replication_health(session, now=NOW + timedelta(minutes=16), state=state)
    assert at_16m["status"] == "degraded"
    assert at_16m["degraded_reason"].startswith("peer hub.pm not delivering for 960s (> 900s)")
    assert "ConnectError" in at_16m["degraded_reason"]


def test_threshold_env_override(session, monkeypatch):
    monkeypatch.setenv("SNOWLINE_REPLICATION_UNREACHABLE_AFTER_S", "60")
    state = DeliveryHealth()
    _setup(session)
    emit.emit_event(session, "thing.recorded", {})
    session.commit()
    _deliver(session, _down, NOW, state)
    assert replication_health(session, now=NOW + timedelta(seconds=59), state=state)["status"] == "ok"
    assert replication_health(session, now=NOW + timedelta(seconds=61), state=state)["status"] == "degraded"
    monkeypatch.setenv("SNOWLINE_REPLICATION_UNREACHABLE_AFTER_S", "not-a-number")
    assert replication_health(session, now=NOW + timedelta(seconds=61), state=state)["status"] == "ok"


def test_retired_subscription_peer_drops_out(session):
    """Retiring the stale stream toward a parked peer clears the degradation."""
    state = DeliveryHealth()
    _setup(session)
    emit.emit_event(session, "thing.recorded", {})
    session.commit()
    _deliver(session, _down, NOW, state)
    late = NOW + timedelta(hours=2)
    assert replication_health(session, now=late, state=state)["status"] == "degraded"
    sub_id = emit.list_outbound_subscriptions(session)[0]["id"]
    emit.retire_outbound_subscription(session, sub_id)
    session.commit()
    block = replication_health(session, now=late, state=state)
    assert block["status"] == "ok"
    assert block["outbox"]["peers"] == {}
    assert block["outbox"]["pending"] == 0


def test_tick_errors_degrade_at_three_consecutive():
    state = DeliveryHealth()
    err = OverflowError("int too large to convert to float")
    state.record_tick(NOW, err)
    state.record_tick(NOW, err)
    block = replication_health(None, now=NOW, state=state)
    assert block["status"] == "ok"
    assert block["outbox"]["tick"] == {
        "last_at": NOW.isoformat() + "Z",
        "last_error": "OverflowError: int too large to convert to float",
        "consecutive_errors": 2,
    }
    state.record_tick(NOW, err)
    block = replication_health(None, now=NOW, state=state)
    assert block["status"] == "degraded"
    assert block["degraded_reason"] == (
        "delivery tick raised 3 consecutive times: "
        "OverflowError: int too large to convert to float"
    )
    # A clean tick clears the run.
    state.record_tick(NOW)
    block = replication_health(None, now=NOW, state=state)
    assert block["status"] == "ok"
    assert block["outbox"]["tick"]["last_error"] is None
    assert block["outbox"]["tick"]["consecutive_errors"] == 0


def test_loop_captures_an_exception_escaping_the_tick(session, monkeypatch):
    """Simulate the #235 shape: the tick itself raises. The loop survives, and
    each raising run lands in DELIVERY_HEALTH."""
    monkeypatch.setenv("SNOWLINE_REPLICATION_INTERVAL", "0")

    def boom(*a, **k):
        raise OverflowError("int too large to convert to float")

    monkeypatch.setattr(emit, "deliver_pending", boom)

    @contextmanager
    def scope():
        yield session

    async def go():
        with anyio.move_on_after(1.0):
            async with anyio.create_task_group() as tg:
                tg.start_soon(emit.replication_delivery_loop, scope)
                while DELIVERY_HEALTH.tick.consecutive_errors < 3:
                    await anyio.sleep(0.01)
                tg.cancel_scope.cancel()

    anyio.run(go)
    block = replication_health(session, state=DELIVERY_HEALTH)
    assert block["outbox"]["tick"]["consecutive_errors"] >= 3
    assert block["status"] == "degraded"
    assert "OverflowError" in block["degraded_reason"]


def test_db_failure_answers_from_memory(make_instance):
    state = DeliveryHealth()
    state.record_failure("hub.pm", NOW, "boom")

    @contextmanager
    def broken_scope():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    block = replication_health_from_scope(broken_scope, now=NOW, state=state)
    assert block["outbox"]["unavailable"] == "no session"
    assert block["outbox"]["pending"] is None
    assert block["outbox"]["peers"]["hub.pm"]["consecutive_failures"] == 1


def test_apply_to_health_merges_and_degrades():
    body = {"status": "ok", "plugin": "pm", "replication": {"dual_primary": {"count": 0}}}
    ok = {"status": "ok", "degraded_reason": None, "outbox": {}}
    out = apply_to_health(dict(body, replication=dict(body["replication"])), ok)
    assert out["status"] == "ok"
    assert "degraded_reason" not in out
    assert out["replication"]["dual_primary"] == {"count": 0}
    assert out["replication"]["outbox"] == {}

    bad = {"status": "degraded", "degraded_reason": "peer x not delivering", "outbox": {}}
    out = apply_to_health({"status": "ok"}, bad)
    assert out["status"] == "degraded"
    assert out["degraded_reason"] == "peer x not delivering"
    assert out["replication"]["status"] == "degraded"


@pytest.fixture(autouse=True)
def _interval(monkeypatch):
    monkeypatch.setenv("SNOWLINE_REPLICATION_INTERVAL", "30")


# --- restart seeding of unreachable_since from the stuck outbox row (#269) ----


def _stuck_row(session, created_at, attempts):
    from sqlalchemy import select

    from snowline_plugin_sdk.replication.models import ReplicationOutboxRow

    emit.emit_event(session, "thing.recorded", {})
    session.commit()
    row = session.execute(select(ReplicationOutboxRow)).scalars().one()
    row.created_at = created_at
    row.attempts = attempts
    session.commit()
    return row


def test_restart_with_old_stuck_row_degrades_on_first_health_call(session):
    _setup(session)
    stamp = NOW - timedelta(days=26)
    _stuck_row(session, stamp, attempts=1025)
    block = replication_health(session, now=NOW, state=DeliveryHealth())
    peer = block["outbox"]["peers"]["hub.pm"]
    assert peer["unreachable_since"] == stamp.isoformat() + "Z"
    assert peer["consecutive_failures"] == 1025
    assert block["status"] == "degraded"
    assert "peer hub.pm not delivering" in block["degraded_reason"]


def test_seed_only_fills_missing_in_memory_value(session):
    _setup(session)
    _stuck_row(session, NOW - timedelta(hours=1), attempts=3)
    # In-memory value OLDER than the stamp: kept.
    state = DeliveryHealth()
    old = NOW - timedelta(hours=5)
    state.record_failure("hub.pm", old, "boom")
    peer = replication_health(session, now=NOW, state=state)["outbox"]["peers"]["hub.pm"]
    assert peer["unreachable_since"] == old.isoformat() + "Z"
    # Missing: filled from the row.
    peer = replication_health(session, now=NOW, state=DeliveryHealth())["outbox"]["peers"]["hub.pm"]
    assert peer["unreachable_since"] == (NOW - timedelta(hours=1)).isoformat() + "Z"
    # Post-restart first failure (in-memory NEWER than the stuck row): the
    # durable, earlier evidence wins so the wedge is not reset to the restart.
    state = DeliveryHealth()
    state.record_failure("hub.pm", NOW - timedelta(minutes=1), "boom")
    block = replication_health(session, now=NOW, state=state)
    assert block["outbox"]["peers"]["hub.pm"]["unreachable_since"] == (
        NOW - timedelta(hours=1)
    ).isoformat() + "Z"
    assert block["status"] == "degraded"


def test_success_clears_seeded_unreachable(session):
    _setup(session)
    _stuck_row(session, NOW - timedelta(days=1), attempts=5)
    state = DeliveryHealth()
    assert replication_health(session, now=NOW, state=state)["status"] == "degraded"
    assert _deliver(session, _ok, NOW + timedelta(minutes=1), state) == 1
    block = replication_health(session, now=NOW + timedelta(minutes=1), state=state)
    assert block["outbox"]["peers"]["hub.pm"]["unreachable_since"] is None
    assert block["status"] == "ok"


def test_no_seed_when_rows_have_zero_attempts(session):
    _setup(session)
    _stuck_row(session, NOW - timedelta(days=26), attempts=0)
    block = replication_health(session, now=NOW, state=DeliveryHealth())
    peer = block["outbox"]["peers"]["hub.pm"]
    assert peer["pending"] == 1
    assert peer["unreachable_since"] is None
    assert block["status"] == "ok"


def test_seed_measured_against_db_clock(session):
    """No `now` override: age comes from the DB clock, so a row stamped 2h ago
    in the DB's frame seeds a ~2h-old unreachable_since in naive UTC."""
    from sqlalchemy import select

    from snowline_plugin_sdk.replication.health import _db_clock, _utcnow

    _setup(session)
    db_now = session.execute(select(_db_clock(session))).scalar_one()
    _stuck_row(session, db_now - timedelta(hours=2), attempts=2)
    block = replication_health(session, state=DeliveryHealth())
    assert block["status"] == "degraded"
    since = datetime.fromisoformat(
        block["outbox"]["peers"]["hub.pm"]["unreachable_since"].rstrip("Z")
    )
    age = (_utcnow() - since).total_seconds()
    assert 2 * 3600 - 30 < age < 2 * 3600 + 30
