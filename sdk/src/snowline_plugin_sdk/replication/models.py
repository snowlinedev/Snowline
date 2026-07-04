"""The SDK-owned replication tables (replication-continuity spec §3, issue #77).

Each opted-in plugin hosts these tables in its OWN database (plugins own their
stores, architecture §2) — the SDK owns the *shape*, the plugin's alembic chain
owns the migration that creates them (adoption, §9 items 3-5). One
`ReplicationBase` metadata for the whole set, so a plugin can point a migration
at `ReplicationBase.metadata` (or its tests at `create_all`) without touching
its domain `Base`.

Five tables, mapping §3's fabric one-to-one:

  * `replication_subscriptions` — OUTBOUND: one row per outbound stream to one
    peer ingest. The stream identity `(source_id, epoch)` is stamped at pairing
    (§5) — fixed at creation, NOT re-read from env at emit, so a config change
    can never silently fork a live stream.
  * `replication_outbox` — the transactional-outbox delivery rows (§3): written
    in the domain write's transaction with the EMIT-TIME `seq` already allocated
    (§3.2). `next_attempt_at` is the §3.1 per-row capped backoff state the
    fire-and-forget bus never had.
  * `replication_stream_counters` — the per-stream emit counters, keyed
    `(source_id, epoch)` in their OWN table (not on the subscription row) on
    purpose: §7's seed reads the restored counters to initialize the spoke's
    inbound watermark, then truncates every cloned replication table —
    counters are deliberately NOT in that truncate set (retained rows under a
    foreign `source_id` are inert; emit allocation is `source_id`-keyed).
  * `replication_inbound_streams` — INBOUND: one row per registered stream =
    the receiver's half of the §5 handshake (it MINTS and holds the secret;
    `previous_secret` is the hitless-rotation overlap) fused with the §3.2
    watermark state: `gate_seq` (the delivery gate — parking advances it) and
    `applied_seq` (the contiguous APPLIED frontier — a parked seq pins it; this
    is what `peer_seen` reports). `blocked_seq`/`blocked_attempts` count the
    §8.1 bounded retryable apply failures toward the parking bound.
  * `replication_parked_events` — §8.1: authentic-but-unappliable events moved
    whole out of the stream's path, loud first-class state, re-appliable.

Postgres is the production dialect (JSONB via variant); plain `JSON` keeps the
SDK's own suite runnable on SQLite with no server.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# JSONB on Postgres (the house dialect), portable JSON elsewhere (SQLite tests).
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


class ReplicationBase(DeclarativeBase):
    """One metadata for the SDK replication tables — separate from any plugin's
    domain `Base` so adoption is additive (the plugin migrates these tables into
    its own DB without re-basing its domain models)."""


class ReplicationSubscription(ReplicationBase):
    """One OUTBOUND stream: this instance's `(source_id, epoch)` toward one peer
    ingest endpoint (spec §3.2/§5).

    `source_id`/`epoch` are stamped at pairing and fixed for the row's life —
    the stream identity every outbox row under it carries. `secret` is the
    stream's HMAC key, MINTED BY THE RECEIVER in the §5 handshake and updated
    in place on rotation (delivery-time signing means the queued backlog
    re-signs automatically). `peer_source_id` names the inbound stream whose
    `applied_seq` this stream stamps as `peer_seen` (§3.2 causal context) —
    NULL before the reverse direction is paired (peer_seen 0). Retiring is a
    soft flip (`active` False + `retired_at`), keeping the delivery log; epochs
    are never reused, so `(source_id, epoch)` stays unique across retirements.
    """

    __tablename__ = "replication_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    target_url: Mapped[str] = mapped_column(String, nullable=False)
    secret: Mapped[str] = mapped_column(String, nullable=False)
    # The event vocabulary this stream carries — a JSON list[str].
    event_types: Mapped[list] = mapped_column(JSONColumn, nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    epoch: Mapped[str] = mapped_column(String, nullable=False)
    # The inbound stream (by its source_id) whose applied frontier this stream
    # reports as peer_seen; NULL until the reverse direction exists.
    peer_source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("source_id", "epoch", name="uq_replication_sub_stream"),
    )


class ReplicationOutboxRow(ReplicationBase):
    """One pending/settled delivery on one outbound stream — the transactional
    outbox (§3) with the §3.1 replication retry class.

    Written in the SAME transaction as the domain write it carries, with `seq`
    ALREADY allocated (emit-time, §3.2) and the full v2 envelope frozen into
    `payload` (peer_seen is authoring-time state — it must not drift to
    delivery time). `status` is `pending` | `delivered` | `rejected`: there is
    deliberately NO attempt cap — unreachability retries forever under the
    capped `next_attempt_at` backoff, and `rejected` (the dead-letter terminal
    state) is reserved for a delivered event the receiver REFUSED as invalid
    (bad signature / malformed / unknown stream — a bug, not a partition).
    Ordering refusals and version holds (HTTP 409) never land here.

    RETENTION NOTE: DELIVERED rows are not just a log — plugin apply logic
    reads them against an incoming envelope's `peer_seen` to compute
    concurrency (governance's §6.1 sibling detection and §6 conflict
    warnings: `seq > peer_seen` ⇒ the author hadn't applied that write).
    A pruning job that drops delivered rows would silently blind that
    detection; none exists, and adding one must account for the lowest
    `peer_seen` a peer can still send."""

    __tablename__ = "replication_outbox"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("replication_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The emit-time per-stream seq (§3.2) — NOT NULL from birth, unlike the
    # bus's delivery-time allocation.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    # The complete v2 envelope (event_type, source, epoch, seq, peer_seen,
    # contract_version, payload) — serialized + signed at delivery time.
    payload: Mapped[dict] = mapped_column(JSONColumn, nullable=False)
    # pending | delivered | rejected (dead-letter; rejections only, §3.1)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # §3.1 capped exponential backoff: NULL = due now; the reconnect reset
    # clears it when the target ingest transitions unreachable → reachable.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "seq", name="uq_replication_outbox_sub_seq"
        ),
    )


class ReplicationStreamCounter(ReplicationBase):
    """The per-stream emit counter — `seq` allocation state, keyed by the stream
    `(source_id, epoch)` (§3.2). Incremented under a row lock in the domain
    write's transaction, so the stream's order is fixed at authoring and the
    counter travels with the store in a `pg_dump` (load-bearing for §7 seeding:
    the snapshot provably contains every event up to the counter's value)."""

    __tablename__ = "replication_stream_counters"

    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    epoch: Mapped[str] = mapped_column(String, primary_key=True)
    last_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )


class ReplicationInboundStream(ReplicationBase):
    """One registered INBOUND stream: the receiver's handshake state (§5) fused
    with its watermark state (§3.2).

    Secrets: the receiver MINTS `secret` (a secret only the sender knows can
    never verify); rotation moves it to `previous_secret`, both verify during
    the switch, and the old retires on the first new-signed delivery (§5).

    Watermarks — the two counters §3.2 forces apart once parking exists:
    `gate_seq` is the DELIVERY gate (exactly `gate_seq + 1` is appliable;
    parking advances it so the stream flows), `applied_seq` is the contiguous
    APPLIED frontier (a parked seq PINS it even as later seqs apply past it —
    a max-style counter would blind §6.1's concurrency detection). `peer_seen`
    on outbound envelopes reports `applied_seq`. `blocked_seq`/
    `blocked_attempts` count consecutive retryable apply failures at the gate
    toward the §8.1 parking bound."""

    __tablename__ = "replication_inbound_streams"

    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    epoch: Mapped[str] = mapped_column(String, primary_key=True)
    secret: Mapped[str] = mapped_column(String, nullable=False)
    # Rotation overlap (§5): the pre-rotation secret, accepted alongside the
    # new one until the first new-signed delivery retires it (NULL again).
    previous_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    gate_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    applied_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    blocked_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    blocked_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ReplicationParkedEvent(ReplicationBase):
    """An authentic-but-unappliable event moved WHOLE out of its stream's path
    after the retryable-apply bound (§8.1). The delivery gate advanced past it
    (the stream flows; the park ACKed to the sender) but `applied_seq` did not
    (gated through, not applied). Loud first-class state — surfaced, never just
    a log line; an empty table is the standing invariant to watch. Re-appliable
    via `ingest.reapply_parked` once the cause is fixed."""

    __tablename__ = "replication_parked_events"

    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    epoch: Mapped[str] = mapped_column(String, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONColumn, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    parked_at: Mapped[datetime] = mapped_column(server_default=func.now())
