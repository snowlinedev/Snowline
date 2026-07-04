"""Memory replication convergence (replication-continuity §10 memory criterion,
#80): two complete memory stores — a hub (`primary.memory`) and a spoke
(`roam.memory`) — wired sender→receiver through an httpx transport that drives
the receiver's SDK `ingest_delivery` (no HTTP server), with memory's own
`remember`/`forget` as the local writes and `memory.apply_event` as the apply.

The write model is a per-name last-writer-wins register with tombstoned deletes,
so these tests pin the three §10 memory races:

  * concurrent `remember("x")` converges to the NEWER write on both sides,
  * a tombstoned `forget` beats an OLDER `set`,
  * a NEWER `set` beats the tombstone (resurrects the memory),

plus the SDK emit/ingest adoption itself (a partitioned write lands exactly once
on reconnect, redelivery is a watermark no-op, an applied write never echoes) and
the `source_id` tiebreak for a same-instant race.

The authoring clock is controlled by patching `memory._utcnow`, so "older" and
"newer" are deterministic rather than wall-clock races.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest import mock

import httpx
import pytest
from sqlalchemy import select

from snowline_plugin_sdk.contract import EVENT_MEMORY_FORGOTTEN, EVENT_MEMORY_SET
from snowline_plugin_sdk.replication import emit, ingest
from snowline_plugin_sdk.replication.models import ReplicationOutboxRow

from snowline_memory import memory
from snowline_memory.models import Memory

_BASE = datetime(2026, 7, 4, 12, 0, 0)


def _t(seconds: int) -> datetime:
    """A deterministic authoring timestamp `seconds` past a fixed base."""
    return _BASE + timedelta(seconds=seconds)


@contextmanager
def _authoring(source_id: str, at: datetime):
    """Pin the instance identity (LWW tiebreak + stream source) and the authoring
    clock for one local write."""
    with mock.patch.object(memory, "_utcnow", return_value=at), mock.patch.dict(
        os.environ, {"SNOWLINE_REPLICATION_SOURCE_ID": source_id}
    ):
        yield


class Instance:
    """One complete memory store + its replication identity."""

    def __init__(self, sessions, source_id: str):
        self.sessions = sessions
        self.source_id = source_id

    def remember(self, name: str, content: str, at: datetime, **kw) -> dict:
        with _authoring(self.source_id, at):
            with self.sessions() as s:
                out = memory.remember(s, content=content, name=name, **kw)
                s.commit()
        return out

    def forget(self, name: str, at: datetime) -> dict:
        with _authoring(self.source_id, at):
            with self.sessions() as s:
                out = memory.forget(s, name)
                s.commit()
        return out

    def register(self, name: str):
        """The stored register for `name`: (content, forgotten, last_source_id) or
        None. Reads the row directly — tombstones (excluded from the read verbs)
        included, so convergence of the whole register is observable."""
        with self.sessions() as s:
            m = s.scalar(select(Memory).where(Memory.name == name))
            return None if m is None else (m.content, m.forgotten, m.last_source_id)

    def digest_names(self) -> set[str]:
        with self.sessions() as s:
            out = memory.memory_digest(s)
        return {e["name"] for g in out["groups"] for e in g["entries"]}

    def outbox(self) -> list[ReplicationOutboxRow]:
        with self.sessions() as s:
            return list(s.scalars(select(ReplicationOutboxRow)))

    def ingest_transport(self) -> httpx.BaseTransport:
        inst = self

        class _T(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                if request.method == "GET":
                    return httpx.Response(405)  # reachable (POST-only route)
                session = inst.sessions()
                try:
                    status, body = ingest.ingest_delivery(
                        session,
                        request.content,
                        request.headers.get("X-Snowline-Signature"),
                        memory.apply_event,
                    )
                    session.commit()
                finally:
                    session.close()
                return httpx.Response(status, json=body)

        return _T()


def _pair(a: Instance, b: Instance) -> None:
    """The §5 handshake both directions for the memory event vocabulary: each
    receiver registers the inbound stream and MINTS the secret, each sender's
    subscription carries it."""
    for sender, receiver, epoch in ((a, b, "ep-a"), (b, a, "ep-b")):
        with receiver.sessions() as s:
            reg = ingest.register_inbound_stream(s, sender.source_id, epoch)
            s.commit()
        with mock.patch.dict(
            os.environ, {"SNOWLINE_REPLICATION_SOURCE_ID": sender.source_id}
        ):
            with sender.sessions() as s:
                emit.create_outbound_subscription(
                    s,
                    f"http://{receiver.source_id}/events/ingest",
                    reg["secret"],
                    [EVENT_MEMORY_SET, EVENT_MEMORY_FORGOTTEN],
                    epoch=epoch,
                    peer_source_id=receiver.source_id,
                )
                s.commit()


def _deliver(sender: Instance, receiver: Instance) -> int:
    with httpx.Client(transport=receiver.ingest_transport()) as client:
        with sender.sessions() as s:
            n = emit.deliver_pending(s, client, reachability={})
            s.commit()
    return n


def _sync(a: Instance, b: Instance) -> None:
    """One delivery pass each direction — enough to converge when each side
    authored its events before this call (the partition heals)."""
    _deliver(a, b)
    _deliver(b, a)


@pytest.fixture()
def hub_spoke(memory_stores):
    return (
        Instance(memory_stores["repl_a"], "primary.memory"),
        Instance(memory_stores["repl_b"], "roam.memory"),
    )


# --- §10 memory criteria -----------------------------------------------------


def test_concurrent_remember_converges_to_newer(hub_spoke):
    """Concurrent `remember("x")` on both sides during a partition converges to
    the NEWER write on BOTH sides after heal."""
    hub, spoke = hub_spoke
    _pair(hub, spoke)

    hub.remember("x", "from-primary", at=_t(10))
    spoke.remember("x", "from-roam", at=_t(20))  # newer
    _sync(hub, spoke)

    for inst in (hub, spoke):
        content, forgotten, _ = inst.register("x")
        assert (content, forgotten) == ("from-roam", False)


def test_tombstoned_forget_beats_older_set(hub_spoke):
    """A tombstoned `forget` (t=20) beats an OLDER `set` (t=10) authored on the
    peer — both sides converge to the tombstone, and the memory is gone from the
    digest on both."""
    hub, spoke = hub_spoke
    _pair(hub, spoke)

    hub.remember("x", "hub-content", at=_t(5))
    hub.forget("x", at=_t(20))
    spoke.remember("x", "spoke-content", at=_t(10))  # older than the forget
    _sync(hub, spoke)

    for inst in (hub, spoke):
        assert inst.register("x")[1] is True  # tombstone
        assert "x" not in inst.digest_names()


def test_newer_set_beats_tombstone(hub_spoke):
    """A NEWER `set` (t=30) beats the tombstone (forget t=20) — both sides
    resurrect the memory to the newer content."""
    hub, spoke = hub_spoke
    _pair(hub, spoke)

    hub.remember("x", "hub-content", at=_t(5))
    hub.forget("x", at=_t(20))
    spoke.remember("x", "spoke-newer", at=_t(30))  # newer than the forget
    _sync(hub, spoke)

    for inst in (hub, spoke):
        content, forgotten, _ = inst.register("x")
        assert (content, forgotten) == ("spoke-newer", False)
        assert "x" in inst.digest_names()


def test_source_id_tiebreak_on_equal_timestamps(hub_spoke):
    """A same-instant race resolves by the `source_id` tiebreak — deterministic
    and identical on both sides (`roam.memory` > `primary.memory`)."""
    hub, spoke = hub_spoke
    _pair(hub, spoke)

    hub.remember("x", "hub-content", at=_t(10))
    spoke.remember("x", "roam-content", at=_t(10))  # SAME timestamp
    _sync(hub, spoke)

    for inst in (hub, spoke):
        content, _, winner = inst.register("x")
        assert winner == "roam.memory"  # the higher source_id wins the tiebreak
        assert content == "roam-content"


# --- SDK emit/ingest adoption ------------------------------------------------


def test_partitioned_set_replicates_once_and_redelivery_is_noop(hub_spoke):
    """A write authored while partitioned lands on the peer exactly once on
    reconnect (the outbox WAS the offline buffer); a forced redelivery is a
    watermark no-op — applied exactly once."""
    hub, spoke = hub_spoke
    _pair(hub, spoke)

    hub.remember("note", "hello", at=_t(10))
    assert spoke.register("note") is None  # nothing delivered during the partition
    assert [r.status for r in hub.outbox()] == ["pending"]

    assert _deliver(hub, spoke) == 1
    assert spoke.register("note") == ("hello", False, "primary.memory")

    # Force a resend — the receiver's gate ACKs it as a duplicate, no re-apply.
    with hub.sessions() as s:
        row = s.scalars(select(ReplicationOutboxRow)).one()
        row.status, row.next_attempt_at = "pending", None
        s.commit()
    assert _deliver(hub, spoke) == 1  # duplicate ACK still counts as delivered
    assert spoke.register("note") == ("hello", False, "primary.memory")


def test_forget_replicates_as_tombstone(hub_spoke):
    """`forget` propagates as a `memory.forgotten` event that tombstones the peer
    copy — the memory leaves the peer's digest."""
    hub, spoke = hub_spoke
    _pair(hub, spoke)

    hub.remember("gone", "here", at=_t(5))
    assert _deliver(hub, spoke) == 1
    assert "gone" in spoke.digest_names()

    hub.forget("gone", at=_t(20))
    assert _deliver(hub, spoke) == 1
    assert spoke.register("gone")[1] is True
    assert "gone" not in spoke.digest_names()


def test_applied_write_never_echoes(hub_spoke):
    """Origin suppression (§3.2 hard rule): after the spoke applies a hub write,
    the spoke's outbox stays empty — the applied write does not re-emit onto the
    spoke→hub stream."""
    hub, spoke = hub_spoke
    _pair(hub, spoke)

    hub.remember("n", "c", at=_t(10))
    assert _deliver(hub, spoke) == 1
    assert spoke.outbox() == []  # no echo


def test_redelivered_forget_is_idempotent(hub_spoke):
    """Re-applying the same `memory.forgotten` is a no-op — LWW ties the stored
    clock, so the tombstone state is unchanged."""
    hub, spoke = hub_spoke
    _pair(hub, spoke)

    hub.remember("x", "v", at=_t(5))
    _deliver(hub, spoke)
    hub.forget("x", at=_t(20))
    _deliver(hub, spoke)
    before = spoke.register("x")

    with spoke.sessions() as s:
        env = {
            "event_type": EVENT_MEMORY_FORGOTTEN,
            "payload": {
                "name": "x",
                "event_at": _t(20).isoformat(),
                "source_id": "primary.memory",
            },
        }
        memory.apply_event(s, env)
        s.commit()
    assert spoke.register("x") == before
