"""The EMIT half (replication-continuity §3.2, issue #77): transactional outbox
with EMIT-time per-stream seq, the v2 envelope (source/epoch/seq/peer_seen),
and origin suppression at the emit hook.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from snowline_plugin_sdk.contract import CONTRACT_VERSION
from snowline_plugin_sdk.replication import emit, ingest
from snowline_plugin_sdk.replication.models import (
    ReplicationOutboxRow,
    ReplicationStreamCounter,
)


def _sub(session, **overrides):
    kwargs = dict(
        target_url="http://peer.example/events/ingest",
        secret="s3cr3t",
        event_types=["thing.recorded"],
        epoch="epoch-1",
    )
    kwargs.update(overrides)
    return emit.create_outbound_subscription(
        session, kwargs.pop("target_url"), kwargs.pop("secret"),
        kwargs.pop("event_types"), **kwargs,
    )


def _rows(session):
    return list(
        session.scalars(select(ReplicationOutboxRow).order_by(ReplicationOutboxRow.seq))
    )


def test_emit_allocates_seq_at_emit_time_in_the_domain_transaction(session):
    """Rows land with `seq` ALREADY allocated (contiguous per stream, §3.2's
    amendment to the bus's delivery-time allocation) and the stream counter
    advances with them — all before any delivery loop runs."""
    _sub(session)
    for i in range(3):
        envs = emit.emit_event(session, "thing.recorded", {"n": i})
        assert len(envs) == 1

    rows = _rows(session)
    assert [r.seq for r in rows] == [1, 2, 3]
    assert all(r.status == "pending" for r in rows)
    counter = session.get(ReplicationStreamCounter, ("test.plugin", "epoch-1"))
    assert counter.last_seq == 3


def test_envelope_carries_the_v2_stream_contract(session):
    """The frozen envelope: stream keys `(source, epoch)`, emit-time `seq`,
    `peer_seen`, `contract_version` 2, and the domain body whole under
    `payload` (§3.2)."""
    _sub(session)
    (env,) = emit.emit_event(session, "thing.recorded", {"id": "x", "v": 1})
    assert env == {
        "event_type": "thing.recorded",
        "contract_version": CONTRACT_VERSION,
        "source": "test.plugin",
        "epoch": "epoch-1",
        "seq": 1,
        "peer_seen": 0,
        "payload": {"id": "x", "v": 1},
    }
    assert CONTRACT_VERSION == 2  # the #77 bump, pinned equal by the drift guard
    # The identical envelope is frozen into the outbox row (peer_seen and seq
    # are authoring-time facts — they must not drift to delivery time).
    assert _rows(session)[0].payload == env


def test_streams_are_independent_per_subscription(session):
    """Two outbound streams (different epochs/peers) allocate independent
    contiguous seqs from their own counters."""
    _sub(session, epoch="epoch-a", target_url="http://a.example/ingest")
    _sub(session, epoch="epoch-b", target_url="http://b.example/ingest")
    emit.emit_event(session, "thing.recorded", {"n": 1})
    emit.emit_event(session, "thing.recorded", {"n": 2})

    by_epoch: dict[str, list[int]] = {}
    for row in _rows(session):
        by_epoch.setdefault(row.payload["epoch"], []).append(row.seq)
    assert by_epoch == {"epoch-a": [1, 2], "epoch-b": [1, 2]}


def test_peer_seen_reports_the_inbound_applied_frontier(session):
    """`peer_seen` = the APPLIED frontier (`applied_seq`) of the inbound stream
    from the subscription's peer — stamped at emit (§3.2 causal context)."""
    _sub(session, peer_source_id="peer.plugin")
    reg = ingest.register_inbound_stream(session, "peer.plugin", "peer-epoch")
    # Simulate two applied events on the inbound stream, then emit locally.
    from snowline_plugin_sdk.replication.models import ReplicationInboundStream

    row = session.get(ReplicationInboundStream, ("peer.plugin", "peer-epoch"))
    row.gate_seq = 2
    row.applied_seq = 2
    session.flush()

    (env,) = emit.emit_event(session, "thing.recorded", {"n": 1})
    assert env["peer_seen"] == 2
    assert reg["secret"]  # the handshake returned the minted secret once


def test_event_type_filter_and_retired_subscriptions(session):
    """Only active subscriptions listing the event type match; a retired stream
    stops emitting without losing its rows."""
    sub = _sub(session, event_types=["thing.recorded"])
    _sub(
        session,
        event_types=["other.event"],
        epoch="epoch-2",
        target_url="http://o.example/ingest",
    )
    assert len(emit.emit_event(session, "thing.recorded", {})) == 1

    emit.retire_outbound_subscription(session, sub["id"])
    assert emit.emit_event(session, "thing.recorded", {}) == []
    assert len(_rows(session)) == 1  # the pre-retirement row survives


def test_emit_with_no_subscription_is_a_noop(session):
    assert emit.emit_event(session, "thing.recorded", {}) == []
    assert _rows(session) == []


def test_source_id_env_is_fail_loud(session, monkeypatch):
    """An unset SNOWLINE_REPLICATION_SOURCE_ID raises (a defaulted source id
    would silently fork stream identity between instances, §3)."""
    monkeypatch.delenv("SNOWLINE_REPLICATION_SOURCE_ID")
    with pytest.raises(ValueError, match="SNOWLINE_REPLICATION_SOURCE_ID"):
        emit.create_outbound_subscription(
            session, "http://p/ingest", "s", ["e"], epoch="e1"
        )


def test_origin_suppression_hard_rule(session):
    """§3.2: emission is a NO-OP while the ingest apply path runs — an
    ingest-applied write can never re-emit (the boomerang guard). Exercised
    through the real ingest path in test_replication_roundtrip; this pins the
    emit-side check in isolation."""
    _sub(session)
    with ingest._applying_replicated_event():
        assert ingest.is_applying_replicated_event()
        assert emit.emit_event(session, "thing.recorded", {"n": 1}) == []
    assert _rows(session) == []
    # …and emission resumes once the apply path exits.
    assert len(emit.emit_event(session, "thing.recorded", {"n": 2})) == 1


def test_subscription_listing_never_leaks_secrets(session):
    _sub(session)
    listed = emit.list_outbound_subscriptions(session)
    assert len(listed) == 1
    assert "secret" not in listed[0]
