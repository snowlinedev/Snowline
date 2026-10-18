"""The EMIT half — transactional outbox with emit-time seq, and the delivery
loop with the replication retry class (replication-continuity §3.1/§3.2,
issue #77).

Generalized from governance's `replication.py` (the bus, decision 97907576/#630)
WITH the §3.2 stream contract, which *changes* the emit side rather than
relocating it:

  * `seq` is allocated HERE, at EMIT time, in the domain write's transaction —
    a per-stream counter row incremented under a row lock. The bus deliberately
    allocated at delivery time so a seq collision could never roll back the
    user's write; replication inverts the trade: authoring order IS the stream
    order, and the counter must travel with the store in a `pg_dump` (§7). The
    counter row is created at pairing (subscription creation), so the emit path
    only ever UPDATEs an existing row — no insert race in the hot path.
  * `peer_seen` (the contiguous applied frontier of the inbound stream from the
    subscription's peer) is stamped into the envelope at emit — authoring-time
    causal context (§6.1), frozen into the outbox payload.
  * Signatures stay DELIVERY-time over the exact bytes POSTed (§5 rotation
    correctness — after a secret swap the whole queued backlog re-signs).

The §3.1 retry class replaces the bus's attempt cap: for a replication peer,
being down for two weeks is normal operation, not failure. Unbounded retry with
CAPPED per-row backoff (`next_attempt_at`, exponential to ~interval x 10), a
per-INGEST reachability probe whose unreachable→reachable transition resets the
backoff on that ingest's rows (the probe is load-bearing: on a quiet heal with
every row at the ceiling, a purely reactive reset could never fire the first
delivery), and dead-letter (`rejected`) reserved for REJECTIONS — a delivered
event the receiver refused as invalid. An ORDERING refusal or version hold
(HTTP 409) is retryable by definition and never dead-letters.

Origin suppression (§3.2, hard rule): `emit_event` is a no-op while the ingest
apply path is running (`ingest.is_applying_replicated_event`), so an
ingest-applied write can NEVER re-emit — without this a delivered event
boomerangs between the pair forever.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from snowline_plugin_sdk.replication import ingest as _ingest
from snowline_plugin_sdk.replication.health import (
    DELIVERY_HEALTH,
    DeliveryHealth,
    peer_key,
)
from snowline_plugin_sdk.replication.envelope import (
    REJECTION_REASONS,
    backfill_source_id,
    build_envelope,
    is_backfill_source,
    sign_body,
)
from snowline_plugin_sdk.replication.models import (
    ReplicationOutboxRow,
    ReplicationStreamCounter,
    ReplicationSubscription,
)

log = logging.getLogger("snowline_plugin_sdk.replication.emit")


def source_id_from_env() -> str:
    """This instance's emit identity, instance-qualified `<instance>.<plugin>`
    (§3 — e.g. `roam.governance`), from `SNOWLINE_REPLICATION_SOURCE_ID`. Read
    live (not module-level) so a test/env change is honored. Fail-loud when
    unset: a defaulted source id would silently fork stream identity between
    two instances of the same plugin."""
    source = os.environ.get("SNOWLINE_REPLICATION_SOURCE_ID")
    if not source:
        raise ValueError(
            "SNOWLINE_REPLICATION_SOURCE_ID is not set — replication needs the "
            "instance-qualified <instance>.<plugin> source id (spec §3)"
        )
    return source


def _interval_seconds() -> float:
    return float(os.environ.get("SNOWLINE_REPLICATION_INTERVAL", "30"))


def _utcnow() -> datetime:
    """Naive UTC — the house convention for compared column values (matches the
    monolith ingest's normalization; keeps SQLite/Postgres comparisons sane)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- subscription management (programmatic + the §5 admin surface) -----------


def create_outbound_subscription(
    session: Session,
    target_url: str,
    secret: str,
    event_types: list[str],
    *,
    epoch: str,
    source_id: str | None = None,
    peer_source_id: str | None = None,
) -> dict:
    """Create one outbound stream toward a peer ingest — the SENDING half of the
    §5 pairing handshake (the receiver already minted `secret` and registered
    the inbound side, so the verifying side holds the secret by construction).
    `epoch` comes from that handshake; `source_id` defaults from
    `SNOWLINE_REPLICATION_SOURCE_ID` and is STAMPED onto the row (stream
    identity is fixed at pairing, not re-read at emit). Also creates the
    stream's emit counter row, so the emit hot path never inserts it."""
    sid = source_id or source_id_from_env()
    sub = ReplicationSubscription(
        target_url=target_url,
        secret=secret,
        event_types=list(event_types),
        source_id=sid,
        epoch=epoch,
        peer_source_id=peer_source_id,
        active=True,
    )
    session.add(sub)
    if session.get(ReplicationStreamCounter, (sid, epoch)) is None:
        session.add(
            ReplicationStreamCounter(source_id=sid, epoch=epoch, last_seq=0)
        )
    session.flush()
    return _subscription_dict(sub)


def list_outbound_subscriptions(session: Session) -> list[dict]:
    """Every outbound subscription (active and retired), newest first. Secrets
    are never included (§5: returned once at the handshake, never logged)."""
    rows = session.scalars(
        select(ReplicationSubscription).order_by(
            ReplicationSubscription.created_at.desc(), ReplicationSubscription.id
        )
    ).all()
    return [_subscription_dict(s) for s in rows]


def retire_outbound_subscription(session: Session, subscription_id: str) -> dict:
    """Soft-retire an outbound stream (re-pair/re-seed, §7 step 5): the row and
    its delivery log stay; the delivery loop stops draining it. Raises
    ValueError on an unknown id."""
    sub = session.get(ReplicationSubscription, _as_uuid(subscription_id))
    if sub is None:
        raise ValueError(f"no replication subscription with id {subscription_id!r}")
    sub.active = False
    sub.retired_at = _utcnow()
    session.flush()
    return _subscription_dict(sub)


def set_subscription_secret(
    session: Session, subscription_id: str, secret: str
) -> dict:
    """The SENDER's half of §5 rotation: swap in the replacement secret the
    receiver minted. Delivery-time signing makes the swap hitless — every still-
    queued row re-signs with the new secret on its next attempt, and the
    receiver retires the old secret on the first new-signed delivery."""
    sub = session.get(ReplicationSubscription, _as_uuid(subscription_id))
    if sub is None:
        raise ValueError(f"no replication subscription with id {subscription_id!r}")
    sub.secret = secret
    session.flush()
    return _subscription_dict(sub)


def widen_outbound_vocabulary(
    session: Session, subscription_id: str, event_types: Iterable[str]
) -> dict:
    """WIDEN an active outbound stream's event vocabulary in place — the SDK
    half of `snowline replicate refresh-vocabulary` (pm#133).

    A subscription's `event_types` is written once at pairing (§5) from the
    sender's then-declared vocabulary, and the emit hook only queues an event
    whose type is in it. An event type a plugin gains in a later upgrade is
    therefore silently never queued toward an existing pairing, and a
    fresh-epoch re-seed cannot recover it (the rows were never written to the
    outbox, so there is nothing to replay). This set-unions `event_types` into
    the subscription instead, so every emit AFTER the widen carries the new
    type on the SAME stream (same epoch, same seq counter — no re-seed).

    Semantics, all load-bearing:
      * set UNION, never narrowing — existing types keep their order, new ones
        are appended in the given order (duplicates collapsed). Removing a type
        from a live stream is not a supported operation here.
      * idempotent — nothing new is a no-op (`added == []`, no write).
      * refused (`ValueError`) for an unknown id, a RETIRED subscription, or a
        §7.1 BACKFILL stream (whose vocabulary is empty by construction: a live
        write fanned onto the walker's counter would corrupt it).

    Which types to widen to is the CALLER's judgement: the pairing tooling
    passes sender-events ∩ receiver-events, so a sender never starts emitting
    a type its peer cannot apply yet (governance decision 54447516, N−1 skew).

    Returns the subscription dict plus `added`: the types actually appended."""
    try:
        key = _as_uuid(subscription_id)
    except ValueError:
        key = None
    sub = session.get(ReplicationSubscription, key) if key is not None else None
    if sub is None:
        raise ValueError(f"no replication subscription with id {subscription_id!r}")
    if not sub.active:
        raise ValueError(
            f"replication subscription {subscription_id!r} is retired — widen "
            f"the live stream that replaced it instead"
        )
    if is_backfill_source(sub.source_id):
        raise ValueError(
            f"replication subscription {subscription_id!r} is a §7.1 backfill "
            f"stream; its vocabulary is empty by construction and cannot be widened"
        )
    current = list(sub.event_types or [])
    seen = set(current)
    added: list[str] = []
    for event_type in event_types:
        if event_type not in seen:
            seen.add(event_type)
            added.append(event_type)
    if added:
        # Reassign (never mutate in place): a plain JSON column does not track
        # in-place list mutation, so an append would be silently dropped.
        sub.event_types = current + added
        session.flush()
    return {**_subscription_dict(sub), "added": added}


def _as_uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _subscription_dict(sub: ReplicationSubscription) -> dict:
    return {
        "id": str(sub.id),
        "target_url": sub.target_url,
        "event_types": list(sub.event_types or []),
        "source_id": sub.source_id,
        "epoch": sub.epoch,
        "peer_source_id": sub.peer_source_id,
        "active": sub.active,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
        "retired_at": sub.retired_at.isoformat() if sub.retired_at else None,
    }


# --- the transactional outbox (emit-time seq, §3.2) ---------------------------


def emit_event(session: Session, event_type: str, payload: dict) -> list[dict]:
    """Write one outbox row per matching outbound stream, IN the caller's domain
    transaction (transactional outbox, §3 — the offline-write buffer). Returns
    the envelopes written (empty when nothing matched or emission is
    suppressed).

    Per stream: `seq` = the stream counter's next value, incremented under a
    row lock (the §3.2 emit-time allocation); `peer_seen` = the applied frontier
    of the inbound stream from `peer_source_id` (0 before the reverse direction
    is paired). The full envelope is frozen into the row — both are
    authoring-time facts and must not drift to delivery time.

    ORIGIN SUPPRESSION (hard rule, §3.2): while the SDK ingest apply path is
    running, this is a no-op — events exist only for locally-originated writes,
    so a replicated write can never boomerang back onto the wire."""
    if _ingest.is_applying_replicated_event():
        return []

    # Deterministic stream order (source_id, epoch) BEFORE taking the counter
    # row locks below: two concurrent domain writes matching 2+ streams would
    # otherwise lock the counters in different orders and deadlock on Postgres.
    subs = session.scalars(
        select(ReplicationSubscription)
        .where(ReplicationSubscription.active.is_(True))
        .order_by(
            ReplicationSubscription.source_id, ReplicationSubscription.epoch
        )
    ).all()
    # A §7.1 backfill stream is invisible to the emit hook BY CONSTRUCTION: its
    # vocabulary is empty, and the identity check is the belt to that braces —
    # a live write fanned onto the walker's counter would interleave
    # non-deterministic seqs and wear backfill causal context.
    live = [s for s in subs if not is_backfill_source(s.source_id)]
    matching = [s for s in live if event_type in (s.event_types or [])]
    if len(matching) != len(live):
        _warn_streams_lacking(live, event_type)
    if not matching:
        return []

    envelopes: list[dict] = []
    for sub in matching:
        counter = session.get(
            ReplicationStreamCounter,
            (sub.source_id, sub.epoch),
            with_for_update=True,
        )
        if counter is None:
            # Defensive: the counter is created with the subscription; a missing
            # row means a hand-created subscription — heal rather than fail the
            # domain write.
            counter = ReplicationStreamCounter(
                source_id=sub.source_id, epoch=sub.epoch, last_seq=0
            )
            session.add(counter)
            session.flush()
        counter.last_seq += 1
        envelope = build_envelope(
            event_type,
            payload,
            source_id=sub.source_id,
            epoch=sub.epoch,
            seq=counter.last_seq,
            peer_seen=_peer_seen(session, sub.peer_source_id),
        )
        session.add(
            ReplicationOutboxRow(
                subscription_id=sub.id,
                seq=counter.last_seq,
                event_type=event_type,
                payload=envelope,
                status="pending",
            )
        )
        envelopes.append(envelope)
    session.flush()
    return envelopes


# (source_id, epoch, event_type) triples already warned about by
# `_warn_streams_lacking` — process-lifetime, so the warning fires once per
# stream and type rather than on every write. A plain set: the check runs on
# the emit hot path and must stay a hash lookup.
_VOCABULARY_WARNED: set[tuple[str, str, str]] = set()

REFRESH_VOCABULARY_REMEDY = "snowline replicate refresh-vocabulary"


def _warn_streams_lacking(live_subs, event_type: str) -> None:
    """WARN once per (source_id, epoch, event_type) for an ACTIVE live stream
    whose frozen vocabulary lacks `event_type` — the write itself commits
    locally, it just does not replicate to that peer. Generic version of pm's
    `_warn_pairings_lacking` so every plugin gets it.

    This is expected (and the warning informational) when the PEER cannot apply
    the type yet: `refresh-vocabulary` only widens to types both sides declare,
    so the remedy is to upgrade the peer and then run it."""
    for sub in live_subs:
        if event_type in (sub.event_types or []):
            continue
        key = (sub.source_id, sub.epoch, event_type)
        if key in _VOCABULARY_WARNED:
            continue
        _VOCABULARY_WARNED.add(key)
        log.warning(
            "replication: the stream %s (epoch %s) toward %s does not carry "
            "%r, so this write does not replicate to it (its event_types were "
            "fixed at pairing). Remedy: upgrade both sides, then run `%s` to "
            "widen the stream; writes made before that are not replayed.",
            sub.source_id, sub.epoch, sub.peer_source_id or sub.target_url,
            event_type, REFRESH_VOCABULARY_REMEDY,
        )


def _peer_seen(session: Session, peer_source_id: str | None) -> int:
    """The contiguous APPLIED frontier (`applied_seq`, not the delivery gate —
    §3.2: a parked seq pins it) of the active inbound stream from
    `peer_source_id`. 0 when the reverse direction isn't paired yet."""
    if peer_source_id is None:
        return 0
    from snowline_plugin_sdk.replication.models import ReplicationInboundStream

    row = session.scalars(
        select(ReplicationInboundStream)
        .where(
            ReplicationInboundStream.source_id == peer_source_id,
            ReplicationInboundStream.active.is_(True),
        )
        .order_by(ReplicationInboundStream.created_at.desc())
    ).first()
    return row.applied_seq if row is not None else 0


# --- §7.1 backfill streams: the direct-enqueue walker ---------------------------
#
# A cold-start union (spec §7.1, #98) manufactures the events two unpaired
# instances never emitted: the walker replays one instance's domain rows, via
# the plugin's ENUMERATOR, as synthetic events on a reserved-identity stream
# (`<live source_id>#backfill`, fresh epoch) that the ORDINARY delivery loop then
# drains through the peer's EXISTING ingest path. The stream is a plain
# `ReplicationSubscription` with three deliberate differences, each load-bearing:
#
#   * EMPTY vocabulary — `emit_event` never fans a live write onto it (a live
#     event on the walker's counter would make seqs non-deterministic and wear
#     backfill causal context). The delivery loop drains its rows regardless.
#   * `peer_source_id = None`, and the walker stamps `peer_seen` 0 itself —
#     the truth for pre-pairing writes (their authors provably saw none of the
#     receiver's stream), and nothing ever consults the live `_peer_seen`
#     lookup for a backfill envelope.
#   * Rows are written DIRECTLY by the walker, in deterministic enumeration
#     order, with seq allocated at ENQUEUE time — the whole walk is ONE
#     transaction (on Postgres pinned to a REPEATABLE READ snapshot), so the
#     enumerated set is frozen at a single instant AFTER pairing opened
#     (overlap-inclusive by construction: a write the live stream also carried
#     simply no-ops on apply) and an aborted open leaves nothing behind.

BackfillEnumerator = Callable[[Session], Iterable[tuple[str, dict]]]

# Flush cadence for the walker's outbox inserts — keeps the ORM unit of work
# bounded on a large store without splitting the walk's single transaction.
_BACKFILL_FLUSH_EVERY = 500


def _pin_snapshot(session: Session) -> None:
    """Pin the walk to ONE snapshot on Postgres (REPEATABLE READ) when the
    session's transaction has not begun yet — READ COMMITTED would give every
    enumerator query its own snapshot. A hardening, not a correctness
    requirement: the bound is overlap-inclusive and apply is idempotent, so a
    row committed mid-walk only adds a harmless no-op."""
    try:
        bind = session.get_bind()
    except Exception:  # noqa: BLE001 - an unbound session: nothing to pin
        return
    if bind.dialect.name != "postgresql" or session.in_transaction():
        return
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def open_backfill_subscription(
    session: Session,
    target_url: str,
    secret: str,
    *,
    epoch: str,
    live_source_id: str,
    enumerator: BackfillEnumerator,
) -> dict:
    """Open one §7.1 backfill stream toward a peer ingest and WALK: create the
    `<live_source_id>#backfill` subscription (empty vocabulary, no peer stream)
    and enqueue every `(event_type, payload)` the plugin's `enumerator` yields
    as an outbox row — seq 1..N in enumeration order, `peer_seen` 0, the
    current-contract envelope with NO extra marker field (the stream identity
    is the marker). The SENDING half of a §5-shaped handshake: the receiver has
    already registered `(<live>#backfill, epoch)` and minted `secret`.

    Read-only on the domain (the walk only reads rows; only the receiver's
    apply writes). Refuses (ValueError) while another ACTIVE backfill toward the
    same target exists — one backfill per direction at a time (the receiver's
    single-active-stream invariant is the real guarantee). Returns the
    subscription record plus `final_seq` (the completion target the union
    verifies the receiver's `applied_seq` against) and per-event-type counts."""
    sid = backfill_source_id(live_source_id)
    _pin_snapshot(session)
    existing = session.scalars(
        select(ReplicationSubscription).where(
            ReplicationSubscription.source_id == sid,
            ReplicationSubscription.target_url == target_url,
            ReplicationSubscription.active.is_(True),
        )
    ).first()
    if existing is not None:
        raise ValueError(
            f"a backfill stream {sid!r} toward {target_url!r} is already active "
            f"(epoch {existing.epoch!r}) — retire it before opening another "
            f"(one backfill per direction at a time, spec §7.1)"
        )
    sub = ReplicationSubscription(
        target_url=target_url,
        secret=secret,
        event_types=[],
        source_id=sid,
        epoch=epoch,
        peer_source_id=None,
        active=True,
    )
    session.add(sub)
    counter = session.get(ReplicationStreamCounter, (sid, epoch))
    if counter is None:
        counter = ReplicationStreamCounter(source_id=sid, epoch=epoch, last_seq=0)
        session.add(counter)
    session.flush()

    seq = counter.last_seq
    counts: dict[str, int] = {}
    for event_type, payload in enumerator(session):
        seq += 1
        session.add(
            ReplicationOutboxRow(
                subscription_id=sub.id,
                seq=seq,
                event_type=event_type,
                payload=build_envelope(
                    event_type,
                    payload,
                    source_id=sid,
                    epoch=epoch,
                    seq=seq,
                    peer_seen=0,
                ),
                status="pending",
            )
        )
        counts[event_type] = counts.get(event_type, 0) + 1
        if seq % _BACKFILL_FLUSH_EVERY == 0:
            session.flush()
    counter.last_seq = seq
    session.flush()
    log.info(
        "backfill stream %s/%s opened toward %s: %d event(s) enqueued %s",
        sid, epoch, target_url, seq, counts,
    )
    return {**_subscription_dict(sub), "final_seq": seq, "enumerated": counts}


def backfill_status(session: Session) -> list[dict]:
    """Every backfill subscription on this sender (active and retired), newest
    first, with its delivery progress: `final_seq` (what the walker enqueued)
    and the outbox's delivered/pending/rejected counts, plus the head row's last
    error while the stream is stuck. The SENDER-side half of the union's
    completion check — the receiver's `applied_seq` and parked set are the
    other half (`list_inbound_streams` / `list_parked` on the peer)."""
    from sqlalchemy import func

    subs = [
        s
        for s in session.scalars(
            select(ReplicationSubscription).order_by(
                ReplicationSubscription.created_at.desc(), ReplicationSubscription.id
            )
        )
        if is_backfill_source(s.source_id)
    ]
    out = []
    for sub in subs:
        counter = session.get(ReplicationStreamCounter, (sub.source_id, sub.epoch))
        by_status = dict(
            session.execute(
                select(ReplicationOutboxRow.status, func.count())
                .where(ReplicationOutboxRow.subscription_id == sub.id)
                .group_by(ReplicationOutboxRow.status)
            ).all()
        )
        head = session.scalars(
            select(ReplicationOutboxRow)
            .where(
                ReplicationOutboxRow.subscription_id == sub.id,
                ReplicationOutboxRow.status != "delivered",
            )
            .order_by(ReplicationOutboxRow.seq)
        ).first()
        out.append(
            {
                **_subscription_dict(sub),
                "final_seq": counter.last_seq if counter is not None else 0,
                "delivered": by_status.get("delivered", 0),
                "pending": by_status.get("pending", 0),
                "rejected": by_status.get("rejected", 0),
                "head_seq": head.seq if head is not None else None,
                "head_error": head.last_error if head is not None else None,
            }
        )
    return out


def retire_backfill_subscription(session: Session, subscription_id: str) -> dict:
    """Retire one backfill stream on the SENDER (§7.1 step 4 — the receiver's
    inbound registration is retired separately). Refuses (ValueError) an id
    naming a LIVE stream, so a mixed-up id can never retire the forward
    stream through the union's verb."""
    sub = session.get(ReplicationSubscription, _as_uuid(subscription_id))
    if sub is None:
        raise ValueError(f"no replication subscription with id {subscription_id!r}")
    if not is_backfill_source(sub.source_id):
        raise ValueError(
            f"subscription {subscription_id!r} ({sub.source_id}) is a live "
            "stream, not a backfill stream — use the outbound retire verb"
        )
    return retire_outbound_subscription(session, subscription_id)


# --- delivery: the §3.1 replication retry class -------------------------------

# Per-process reachability memory for the reconnect reset: ingest URL → last
# known reachable? A restart forgets it (rows' backoff state is the durable
# half); the first probe after boot repopulates it without resetting anything.
_REACHABILITY: dict[str, bool] = {}


def _backoff(attempts: int) -> timedelta:
    """Exponential from one delivery interval, capped at ~interval x 10 (§3.1):
    a two-week partition retries every ~5 minutes at the default 30s interval,
    never dead-letters, and drains within one interval of the probe seeing the
    heal.

    The exponent is clamped BEFORE doubling (issue #235): `2 ** (attempts - 1)`
    for attempts past ~1024 overflows float in the multiply, so the cap's
    `min()` never ran and retry bookkeeping itself raised every tick. 2**10
    already exceeds the x10 ceiling, so the clamp never changes a result."""
    interval = _interval_seconds()
    return timedelta(seconds=min(interval * (2 ** min(attempts - 1, 10)), interval * 10))


_GATEWAY_DOWN_STATUSES = frozenset({502, 503})


def _is_gateway_unreachable(resp) -> bool:
    """True when a probe response means "the plugin behind the gateway is down".

    Deliveries route through the peer's platform gateway (`/via`, decision
    0b8390f7), so a DOWN plugin no longer refuses the connection: the gateway
    itself answers 503 (`plugin 'pm' is down`) or 502 (upstream unreachable).
    Those are the ONLY statuses the `/via` proxy emits for a down plugin, so
    they count as unreachable; every other status (404, 405, 401, 2xx, ...)
    still means "the ingest is answering" and stays reachable. Without this the
    unreachable → reachable heal transition never fires behind a gateway and a
    returning plugin waits out the full capped backoff (issue #233)."""
    return resp.status_code in _GATEWAY_DOWN_STATUSES


def _probe_ingests(
    client,
    urls: set[str],
    reachability: dict[str, bool],
    errors: dict[str, str] | None = None,
) -> set[str]:
    """Cheaply probe every ingest endpoint that has queued rows; return the set
    that just transitioned unreachable → reachable (whose rows get their backoff
    reset). Any HTTP response counts as reachable — the probe asks "is the
    ingest answering?", not "would a delivery succeed?" (a GET on a POST-only
    route answering 405 is a healthy ingest) — EXCEPT a gateway-originated
    502/503 (`_is_gateway_unreachable`). Per-INGEST, not per-peer, on
    purpose (§3.1): a single plugin's ingest healing on an otherwise-reachable
    peer must trigger the reset too. When `errors` is given, each URL found
    unreachable this probe gets its error text recorded there (for health)."""
    healed: set[str] = set()
    for url in urls:
        was_reachable = reachability.get(url)
        try:
            resp = client.get(url)
            reachable = not _is_gateway_unreachable(resp)
            if not reachable and errors is not None:
                errors[url] = "gateway " + _response_error(resp).removeprefix("HTTP ")
        except Exception as exc:  # noqa: BLE001 - any transport error means unreachable
            reachable = False
            if errors is not None:
                errors[url] = str(exc)
        reachability[url] = reachable
        if reachable and was_reachable is False:
            healed.add(url)
    return healed


def deliver_pending(
    session: Session,
    client,
    *,
    now: datetime | None = None,
    reachability: dict[str, bool] | None = None,
    health: DeliveryHealth | None = None,
) -> int:
    """One delivery pass over every active outbound stream. Returns the count
    newly ACKed (applied / duplicate / parked all count — a park ACKs exactly
    like a success, §8.1).

    Per stream, rows drain IN SEQ ORDER and the drain STOPS at the first
    non-ACK: the per-stream cursor never advances past an undelivered seq
    (§3.2), so a persistently failing delivery blocks only its own stream —
    loud and recoverable — and nothing is skipped to be later discarded as
    already-seen. Only the stream head ever carries backoff state (rows behind
    it are never attempted), so the head's `next_attempt_at` gates the whole
    stream's next try.

    Classification (§3.1): 2xx → delivered. A 4xx carrying the ingest
    vocabulary's rejection body (`{"status": "rejected"}`, reason in
    `REJECTION_REASONS`) → `rejected` (dead-letter; the stream wedges loudly
    behind it, since skipping a seq would only trade the wedge for an
    out-of-order refusal — `requeue_rejected` is the recovery). EVERYTHING
    else — 409 refusals (ordering / version hold, retryable by definition),
    bare 4xx (a trust-gate 403, a 404 before the peer's router is mounted, a
    proxy's 405/429: stream-level conditions, not a verdict on this event —
    treating them as rejections would cascade-destroy the backlog one head
    per tick), 5xx and transport errors → retryable with backoff. Each row's
    outcome commits per-row so progress survives a mid-pass crash
    (re-delivery is a duplicate no-op on the receiver).

    The tick opens with the §3.1 reachability probe; an ingest transitioning
    unreachable → reachable gets its rows' backoff cleared, which is what makes
    §10's "within one delivery interval of reconnect" satisfiable.

    Every delivery outcome is also recorded into `health` (default: the
    process-wide `DELIVERY_HEALTH`) BEFORE the row's retry bookkeeping runs,
    so `/health` sees a failing peer even if that bookkeeping itself raises
    (issue #241 — the #235 overflow was invisible exactly that way)."""
    now = now or _utcnow()
    if reachability is None:
        reachability = _REACHABILITY
    if health is None:
        health = DELIVERY_HEALTH

    subs = session.scalars(
        select(ReplicationSubscription).where(
            ReplicationSubscription.active.is_(True)
        )
    ).all()
    pending_by_sub: dict[uuid.UUID, list[ReplicationOutboxRow]] = {}
    for sub in subs:
        rows = session.scalars(
            select(ReplicationOutboxRow)
            .where(
                ReplicationOutboxRow.subscription_id == sub.id,
                ReplicationOutboxRow.status == "pending",
            )
            .order_by(ReplicationOutboxRow.seq)
        ).all()
        if rows:
            pending_by_sub[sub.id] = list(rows)

    # The reconnect reset (§3.1): probe first, then clear backoff on healed
    # ingests' rows so the backlog flushes THIS tick, not ~10 intervals later.
    queued_urls = {s.target_url for s in subs if s.id in pending_by_sub}
    probe_errors: dict[str, str] = {}
    healed = _probe_ingests(client, queued_urls, reachability, probe_errors)
    # A probe-detected unreachable peer feeds the same per-peer health the
    # delivery loop writes (#268), so /health sees a gateway-503 peer even while
    # its head row is still backing off and no POST is attempted.
    for sub in subs:
        if sub.id not in pending_by_sub or sub.target_url not in probe_errors:
            continue
        head = pending_by_sub[sub.id][0]
        # A DUE head is about to be POSTed, and that attempt records its own
        # outcome; only a backing-off head needs the probe to speak for it.
        if head.next_attempt_at is not None and head.next_attempt_at > now:
            health.record_failure(peer_key(sub), now, probe_errors[sub.target_url])
    if healed:
        for sub in subs:
            if sub.target_url in healed:
                for row in pending_by_sub.get(sub.id, []):
                    row.next_attempt_at = None
        session.commit()

    delivered = 0
    for sub in subs:
        for row in pending_by_sub.get(sub.id, []):
            if row.next_attempt_at is not None and row.next_attempt_at > now:
                break  # head not due — the whole stream waits (contiguity)
            body = json.dumps(row.payload).encode()
            headers = {
                "Content-Type": "application/json",
                "X-Snowline-Event": row.event_type,
                "X-Snowline-Signature": f"sha256={sign_body(sub.secret, body)}",
            }
            try:
                resp = client.post(sub.target_url, content=body, headers=headers)
            except Exception as exc:  # noqa: BLE001 - transport error → retry
                reachability[sub.target_url] = False
                health.record_failure(peer_key(sub), now, exc)
                _record_retry(row, now, str(exc))
                session.commit()
                break
            reachability[sub.target_url] = not _is_gateway_unreachable(resp)
            if 200 <= resp.status_code < 300:
                row.status = "delivered"
                row.delivered_at = now
                row.next_attempt_at = None
                delivered += 1
                health.record_success(peer_key(sub), now)
                session.commit()
                continue
            if _is_vocabulary_rejection(resp):
                # The ingest itself REFUSED this event as invalid — a bug, not
                # a partition. Dead-letter, loudly; the stream wedges behind it
                # until the cause is fixed and the row is requeued.
                row.status = "rejected"
                row.last_error = _response_error(resp)
                health.record_failure(peer_key(sub), now, row.last_error)
                log.error(
                    "replication delivery REJECTED (stream %s/%s seq %s): %s",
                    sub.source_id, sub.epoch, row.seq, row.last_error,
                )
            else:
                # Everything else is retryable: 409 refusals (ordering /
                # version hold — §3.1/§3.2), bare 4xx stream-level conditions,
                # 5xx. See the docstring's classification rationale.
                error = _response_error(resp)
                health.record_failure(peer_key(sub), now, error)
                _record_retry(row, now, error)
            session.commit()
            break  # non-ACK: never advance past an undelivered seq
    return delivered


def _is_vocabulary_rejection(resp) -> bool:
    """True only for a 4xx whose body is the ingest vocabulary's rejection —
    `{"status": "rejected"}` with a reason in `REJECTION_REASONS`. A bare 4xx
    (no parseable body / another shape) is a stream-level condition and stays
    retryable: empirically, a receiver trust-gate 403 would otherwise reject a
    3-row backlog in 3 ticks — a recoverable misconfiguration permanently
    destroying data."""
    if not (400 <= resp.status_code < 500):
        return False
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - any unparseable body is not the vocabulary
        return False
    return (
        isinstance(body, dict)
        and body.get("status") == "rejected"
        and body.get("reason") in REJECTION_REASONS
    )


def _record_retry(row: ReplicationOutboxRow, now: datetime, error: str) -> None:
    row.attempts = (row.attempts or 0) + 1
    row.last_error = error
    row.next_attempt_at = now + _backoff(row.attempts)


def _response_error(resp) -> str:
    try:
        detail = resp.json()
    except Exception:  # noqa: BLE001 - non-JSON error body
        detail = None
    return f"HTTP {resp.status_code}" + (f" {detail}" if detail else "")


# --- dead-letter surfacing (§3.1) ---------------------------------------------
#
# The sender-side mirror of the receiver's parked view: `rejected` rows are
# rare-and-real (only the ingest vocabulary's verdicts land here), the stream
# is wedged behind each one, and an empty list is the standing invariant to
# watch. The admin surface exposes both next to /parked.


class RequeueRefusedError(ValueError):
    """Raised by `requeue_rejected`/`requeue_rejected_bulk` when the target
    subscription is retired (issue #108: rotation is retain-not-refuse, so a
    retired subscription's rows are discoverable but must never be silently
    flipped back to `pending` under it — nothing serves that stream anymore,
    so the row would be undeliverable limbo: not parked, not rejected, never
    delivered). A `ValueError` subclass so any caller that only distinguishes
    "not found" from "success" still sees a 4xx-shaped failure; `admin.py`
    catches this SPECIFIC type first to answer 409 with `.detail` instead of
    the generic 404."""

    def __init__(self, message: str, *, detail: dict):
        super().__init__(message)
        self.detail = detail


# The ingest vocabulary's rejection reason, as embedded by `_response_error`
# into `last_error` (`f"HTTP {code} {resp.json()}"` — a Python dict repr of
# `{"status": "rejected", "reason": "<reason>"}`, since `_is_vocabulary_
# rejection` gates entry into `rejected` status on exactly that shape). No
# separate column: the reason is already fully determined by this string for
# every row that reaches `rejected`, so the bulk requeue's optional reason
# filter parses it out rather than duplicating storage.
_REJECTION_REASON_RE = re.compile(r"'reason':\s*'([^']+)'")


def _rejection_reason(last_error: str | None) -> str | None:
    if not last_error:
        return None
    match = _REJECTION_REASON_RE.search(last_error)
    return match.group(1) if match else None


def _find_active_successor(
    session: Session, sub: ReplicationSubscription
) -> ReplicationSubscription | None:
    """The live stream that replaced a retired one, when — and only when —
    it's unambiguous: the SINGLE active subscription sharing both `source_id`
    (this instance's fixed emit identity) and `target_url` (the same peer).
    Re-pairing with the same peer retires the old row and mints a fresh epoch
    under a new one; `source_id` alone would be ambiguous the moment this
    instance pairs a second, different peer under the same identity. Returns
    None on zero or multiple candidates — the caller still refuses the
    requeue, it just can't name one successor to point at."""
    candidates = session.scalars(
        select(ReplicationSubscription).where(
            ReplicationSubscription.source_id == sub.source_id,
            ReplicationSubscription.target_url == sub.target_url,
            ReplicationSubscription.active.is_(True),
            ReplicationSubscription.id != sub.id,
        )
    ).all()
    return candidates[0] if len(candidates) == 1 else None


def _guard_active_subscription(
    session: Session, sub: ReplicationSubscription
) -> None:
    """Issue #108's guard: refuse a requeue targeting a retired subscription.
    Rotation is retain-not-refuse (the retired row and its delivery log stay,
    ee5794e), so the successor — when unambiguous — is discoverable and named
    in the refusal AS CONTEXT: the rows belong to the retired stream forever
    (there is no retarget API), so the real remedy is re-pair + re-seed (§7
    step 5), which carries the domain state forward under a fresh epoch.

    CALLERS MUST LOAD `sub` WITH `with_for_update=True`: under READ COMMITTED
    a concurrent retire committing between a plain read and the row flip
    would recreate the exact limbo this guard exists to kill — the FOR UPDATE
    lock serializes the guard read against retire's `active=False` write."""
    if sub.active:
        return
    successor = _find_active_successor(session, sub)
    detail = {
        "reason": "subscription_retired",
        "subscription_id": str(sub.id),
        "source_id": sub.source_id,
        "epoch": sub.epoch,
        "retired_at": sub.retired_at.isoformat() if sub.retired_at else None,
    }
    message = (
        f"outbound subscription {sub.id} ({sub.source_id}/{sub.epoch}) is "
        "retired; its rejected rows cannot be requeued — no delivery loop "
        "serves a retired stream. Re-pair (and re-seed if the peer diverged, "
        "spec §7 step 5) to carry this state forward under a fresh epoch"
    )
    if successor is not None:
        detail["successor_subscription_id"] = str(successor.id)
        detail["successor_epoch"] = successor.epoch
        message += (
            f" (an active successor {successor.id} "
            f"({successor.source_id}/{successor.epoch}) already exists for "
            "this peer)"
        )
    raise RequeueRefusedError(message, detail=detail)


def list_rejected(session: Session) -> list[dict]:
    """Every dead-lettered outbox row, oldest first, labelled with its stream
    identity (payload omitted — it can be large; the row id keys the requeue).
    `reason` is the ingest vocabulary's rejection reason, parsed out of
    `last_error` — the same value the bulk requeue's `reason` filter matches
    against."""
    rows = session.execute(
        select(ReplicationOutboxRow, ReplicationSubscription)
        .join(
            ReplicationSubscription,
            ReplicationOutboxRow.subscription_id == ReplicationSubscription.id,
        )
        .where(ReplicationOutboxRow.status == "rejected")
        .order_by(ReplicationOutboxRow.created_at, ReplicationOutboxRow.seq)
    ).all()
    return [
        {
            "id": str(row.id),
            "subscription_id": str(sub.id),
            "source_id": sub.source_id,
            "epoch": sub.epoch,
            "seq": row.seq,
            "event_type": row.event_type,
            "attempts": row.attempts,
            "last_error": row.last_error,
            "reason": _rejection_reason(row.last_error),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row, sub in rows
    ]


def requeue_rejected(session: Session, row_id: str) -> dict:
    """Put one dead-lettered row back on its stream after the cause is fixed
    (the recovery for a wedged stream — the receiver's gate still expects this
    seq, so requeueing the head lets the whole backlog resume). Due
    immediately; `last_error` keeps the rejection until the next outcome
    overwrites it. Raises ValueError on an unknown id or a non-rejected row,
    and `RequeueRefusedError` (issue #108) when the row's subscription has
    since been retired — an operator action, deliberate like
    `reapply_parked`."""
    row = session.get(ReplicationOutboxRow, _as_uuid(row_id))
    if row is None:
        raise ValueError(f"no replication outbox row with id {row_id!r}")
    if row.status != "rejected":
        raise ValueError(
            f"outbox row {row_id!r} is {row.status!r}, not 'rejected'"
        )
    # FOR UPDATE (no-op on SQLite, real on Postgres): serialize the guard read
    # against a concurrent retire — a plain read could pass the guard, the
    # retire commit, and the flip land on a now-retired stream (the #108 limbo
    # recreated through the guarded path).
    sub = session.get(
        ReplicationSubscription, row.subscription_id, with_for_update=True
    )
    if sub is None:
        raise ValueError(
            f"outbox row {row_id!r} has no subscription {row.subscription_id!r}"
        )
    _guard_active_subscription(session, sub)
    row.status = "pending"
    row.attempts = 0
    row.next_attempt_at = None
    session.flush()
    return {"id": str(row.id), "seq": row.seq, "status": row.status}


def requeue_rejected_bulk(
    session: Session,
    subscription_id: str,
    *,
    event_type: str | None = None,
    reason: str | None = None,
) -> dict:
    """Set-at-a-time requeue, scoped to one stream (issue #107): every
    `rejected` row on `subscription_id`, optionally narrowed to one
    `event_type` and/or one rejection `reason`, flips back to `pending` —
    same semantics as `requeue_rejected`, just applied to the whole matching
    set at once. Built for the canonical mass-rejection shape: a producer
    ships a new event type before this consumer upgrades, and every row of
    that type dead-letters until the fix lands — recovery today is one call
    per row; this is one call for the lot. Returns the count requeued.

    RETIRED-SUBSCRIPTION GUARD (issue #108): if the target subscription is
    retired, the WHOLE call refuses via `RequeueRefusedError` — nothing is
    requeued, matching `requeue_rejected`'s per-row refusal. Skip-and-report
    was considered and rejected: a bulk call that silently requeues 0 of N
    rows onto a retired target reads as "nothing needed action" instead of
    "your target moved," which is exactly the silent-limbo failure mode this
    guard exists to kill. Refuse loudly, once, for the whole set.

    `reason` is validated against the CLOSED `REJECTION_REASONS` vocabulary
    (only those three values can ever appear on a rejected row) — a typo'd
    reason answering `{"requeued": 0}` would read as "already handled", the
    same silent failure shape in a different coat. `event_type` is
    deliberately NOT validated: it's open vocabulary per adopter, so a
    zero-match filter there is a legitimate answer, not a typo verdict."""
    if reason is not None and reason not in REJECTION_REASONS:
        raise ValueError(
            f"unknown rejection reason {reason!r}; expected one of "
            f"{sorted(REJECTION_REASONS)}"
        )
    # FOR UPDATE for the same race as requeue_rejected: the guard read must
    # serialize against a concurrent retire's `active=False` write.
    sub = session.get(
        ReplicationSubscription, _as_uuid(subscription_id), with_for_update=True
    )
    if sub is None:
        raise ValueError(
            f"no replication subscription with id {subscription_id!r}"
        )
    _guard_active_subscription(session, sub)

    where = [
        ReplicationOutboxRow.subscription_id == sub.id,
        ReplicationOutboxRow.status == "rejected",
    ]
    if event_type is not None:
        where.append(ReplicationOutboxRow.event_type == event_type)
    values = {"status": "pending", "attempts": 0, "next_attempt_at": None}

    if reason is None:
        # One set-based UPDATE — no ORM materialization (payloads can be
        # large; even list_rejected leaves them out) and no per-row UPDATEs.
        requeued = session.execute(
            update(ReplicationOutboxRow).where(*where).values(**values)
        ).rowcount
    else:
        # The reason filter needs Python (parsed out of last_error), but only
        # (id, last_error) is fetched — never the payload — and the flip is
        # still one UPDATE over the matched id set.
        pairs = session.execute(
            select(ReplicationOutboxRow.id, ReplicationOutboxRow.last_error)
            .where(*where)
        ).all()
        ids = [rid for rid, err in pairs if _rejection_reason(err) == reason]
        if ids:
            session.execute(
                update(ReplicationOutboxRow)
                .where(ReplicationOutboxRow.id.in_(ids))
                .values(**values)
            )
        requeued = len(ids)
    session.flush()
    return {
        "subscription_id": str(sub.id),
        "source_id": sub.source_id,
        "epoch": sub.epoch,
        "requeued": requeued,
    }


async def replication_delivery_loop(
    session_scope: Callable[[], AbstractContextManager[Session]],
    *,
    enabled: bool = True,
) -> None:
    """Background loop draining the replication outbox on a timer
    (`SNOWLINE_REPLICATION_INTERVAL`, default 30s) — the plugin runs it in its
    app lifespan next to its other loops, passing its own `session_scope`.
    Mirrors governance's `webhook_delivery_loop`: one session + one client per
    tick, per-tick exceptions swallowed/logged so a transient outage can never
    kill the loop.

    `enabled` is the SDK's first-class defer/gate seam (issue #91): the loop
    used to fire its first tick immediately with no built-in way to keep a
    freshly booted app quiet, so every adopter re-invented the same
    app-level boolean purely to keep test-app boots from doing real DB/network
    activity — the platform's `replicate` flag on `create_app` (mirroring its
    pre-existing `poll_health` switch), memory's `replicate_on_startup`. Pass
    `enabled=False` (typically by forwarding the adopter's own opt-in flag
    straight through) and the coroutine returns immediately without touching
    the session or the network — same convention as `poll_health`. Defaults to
    `True` so a bare `tg.start_soon(replication_delivery_loop, session_scope)`
    keeps ticking exactly as it always has; no existing caller's behavior
    changes.

    `SNOWLINE_REPLICATION_DISABLED` remains a supported, blunter, process-wide
    version of the same gate (an operator killswitch, or a test suite that
    pins the env var instead of threading a parameter — e.g. governance's
    autouse fixture); either one disables the loop, so migrating to `enabled`
    is opt-in, not required."""
    import anyio
    import httpx

    if not enabled:
        log.info("replication delivery disabled (enabled=False)")
        return
    if os.environ.get("SNOWLINE_REPLICATION_DISABLED"):
        log.info("replication delivery disabled via SNOWLINE_REPLICATION_DISABLED")
        return

    def _tick() -> None:
        # follow_redirects stays OFF: a 307/308 would re-POST the signed body
        # to a location the subscription never named; a redirecting front is a
        # misconfiguration surfaced as a retryable 3xx, not silently followed.
        with httpx.Client(timeout=20.0) as client:
            with session_scope() as session:
                n = deliver_pending(session, client)
        if n:
            log.info("replication delivery: delivered=%d", n)

    while True:
        try:
            await anyio.to_thread.run_sync(_tick)
        except Exception as exc:  # noqa: BLE001 - the loop must outlive any one tick
            # Recorded for /health (issue #241): >= 3 consecutive raising
            # ticks degrade the plugin instead of only filling the log.
            DELIVERY_HEALTH.record_tick(_utcnow(), exc)
            log.exception("replication delivery tick failed; loop continues")
        else:
            DELIVERY_HEALTH.record_tick(_utcnow())
        await anyio.sleep(_interval_seconds())
