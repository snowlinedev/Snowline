"""Replication delivery HEALTH — the outbox state a plugin's `/health` carries
(issue #241, replication-continuity §3.1 "Delivery health").

Why this exists: the delivery loop swallows per-tick exceptions so a transient
fault can never kill it — which is right — but that also meant a loop failing
on EVERY tick for weeks (the #235 backoff overflow toward a parked spoke) left
`/health` green and the platform aggregate green. This module makes that state
visible and turns a sustained failure into a `degraded` health status.

Two halves:

  * **In-memory delivery state** (`DeliveryHealth`), fed by the delivery loop:
    per peer — consecutive non-ACK deliveries, last success, last error,
    `unreachable_since` (the first failure of the current failure run; cleared
    by any ACK) — and per tick — last run, whether it raised, consecutive
    raising ticks. Per-process: a restart forgets it (the DB half survives).
  * **DB-derived outbox state**: ONE grouped query over `replication_outbox`
    (pending count, oldest pending `created_at`, head attempts per active
    subscription) per health call. `consecutive_failures` reports the larger
    of the in-memory run and the head row's `attempts`, so a restart doesn't
    zero a long-wedged stream's failure count.

Degrade rule (`status: "degraded"`, never `"down"` — the plugin still serves
and `/health` still answers 200, so the gateway keeps routing to it):

  * any active peer's `unreachable_since` is older than
    `SNOWLINE_REPLICATION_UNREACHABLE_AFTER_S` (default 900 = 15 min), or
  * the delivery tick itself has raised on >= 3 consecutive runs.

Status vocabulary (health.md): `ok` | `degraded` | `down` (`down` is never
self-reported — it is the platform's verdict on a non-2xx/unreachable health
endpoint).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import DateTime, func, select
from sqlalchemy.orm import Session

from snowline_plugin_sdk.replication.models import (
    ReplicationOutboxRow,
    ReplicationSubscription,
)

DEFAULT_UNREACHABLE_AFTER_S = 900.0
TICK_ERRORS_DEGRADE_AT = 3
_ERROR_MAX_CHARS = 300


def unreachable_after_seconds() -> float:
    """The degrade threshold for a non-delivering peer, read live so a test /
    env change is honored. A malformed value falls back to the default rather
    than making `/health` raise."""
    raw = os.environ.get("SNOWLINE_REPLICATION_UNREACHABLE_AFTER_S")
    if raw is None or raw.strip() == "":
        return DEFAULT_UNREACHABLE_AFTER_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_UNREACHABLE_AFTER_S


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def format_error(error: BaseException | str) -> str:
    """`<ExceptionClass>: <message>`, truncated — health payloads stay small and
    never carry a full traceback."""
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    else:
        text = str(error)
    if len(text) > _ERROR_MAX_CHARS:
        text = text[: _ERROR_MAX_CHARS - 1] + "…"
    return text


def peer_key(sub: ReplicationSubscription) -> str:
    """How a stream's peer is named in the health block: its paired
    `peer_source_id` when the reverse direction exists, else the ingest URL."""
    return sub.peer_source_id or sub.target_url


@dataclass
class _PeerState:
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_error: str | None = None
    unreachable_since: datetime | None = None


@dataclass
class _TickState:
    last_at: datetime | None = None
    last_error: str | None = None
    consecutive_errors: int = 0


@dataclass
class DeliveryHealth:
    """Thread-safe in-memory delivery state (ticks run in a worker thread;
    `/health` reads from the event loop)."""

    peers: dict[str, _PeerState] = field(default_factory=dict)
    tick: _TickState = field(default_factory=_TickState)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_success(self, peer: str, now: datetime) -> None:
        with self._lock:
            st = self.peers.setdefault(peer, _PeerState())
            st.consecutive_failures = 0
            st.last_success_at = now
            st.unreachable_since = None

    def record_failure(self, peer: str, now: datetime, error: BaseException | str) -> None:
        with self._lock:
            st = self.peers.setdefault(peer, _PeerState())
            st.consecutive_failures += 1
            st.last_error = format_error(error)
            if st.unreachable_since is None:
                st.unreachable_since = now

    def record_tick(self, now: datetime, error: BaseException | None = None) -> None:
        with self._lock:
            self.tick.last_at = now
            if error is None:
                self.tick.last_error = None
                self.tick.consecutive_errors = 0
            else:
                self.tick.last_error = format_error(error)
                self.tick.consecutive_errors += 1

    def reset(self) -> None:
        with self._lock:
            self.peers.clear()
            self.tick = _TickState()

    def _snapshot(self) -> tuple[dict[str, _PeerState], _TickState]:
        with self._lock:
            peers = {k: _PeerState(**vars(v)) for k, v in self.peers.items()}
            return peers, _TickState(**vars(self.tick))


# The process-wide state the delivery loop writes and `/health` reads.
DELIVERY_HEALTH = DeliveryHealth()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt is not None else None


def _db_clock(session: Session):
    """The database's own clock in the SAME frame `created_at` was stored in.
    `created_at` is a naive `server_default=now()` column, so on Postgres it
    holds the SESSION-timezone wall clock (America/Detroit on the hub, not
    UTC); comparing it against Python's naive UTC would skew the age by the
    zone offset. `localtimestamp` is that same frame; SQLite's
    CURRENT_TIMESTAMP is UTC, matching its `now()` default."""
    if session.get_bind().dialect.name == "postgresql":
        return func.localtimestamp(type_=DateTime)
    return func.current_timestamp(type_=DateTime)


def _outbox_rows(session: Session) -> list[tuple]:
    """The ONE grouped query: per ACTIVE subscription, its pending count,
    oldest pending `created_at`, and the head's attempts (only the stream head
    is ever attempted, so max(attempts) over pending rows is the head's), plus
    the DB clock to age `created_at` against."""
    pending = (
        select(
            ReplicationOutboxRow.subscription_id.label("sid"),
            func.count().label("n"),
            func.min(ReplicationOutboxRow.created_at).label("oldest"),
            func.max(ReplicationOutboxRow.attempts).label("attempts"),
        )
        .where(ReplicationOutboxRow.status == "pending")
        .group_by(ReplicationOutboxRow.subscription_id)
        .subquery()
    )
    return list(
        session.execute(
            select(
                ReplicationSubscription.peer_source_id,
                ReplicationSubscription.target_url,
                pending.c.n,
                pending.c.oldest,
                pending.c.attempts,
                _db_clock(session).label("db_now"),
            )
            .select_from(ReplicationSubscription)
            .outerjoin(pending, pending.c.sid == ReplicationSubscription.id)
            .where(ReplicationSubscription.active.is_(True))
        ).all()
    )


def replication_health(
    session: Session | None,
    *,
    now: datetime | None = None,
    state: DeliveryHealth | None = None,
) -> dict:
    """The `replication` health block (shape in the module docstring):

        {"status": "ok"|"degraded", "degraded_reason": str|None,
         "outbox": {"pending": N, "oldest_pending_age_s": X|None,
                    "peers": {<peer>: {pending, consecutive_failures,
                                       last_success_at, last_error,
                                       unreachable_since}},
                    "tick": {last_at, last_error, consecutive_errors}}}

    `session=None` (or a failing query — e.g. the DB is down) still answers
    from the in-memory half, with `outbox.unavailable` naming the error, so
    `/health` never raises on this. Only ACTIVE subscriptions' peers appear
    (retiring a stale stream clears its degradation).

    `now` overrides the clock for BOTH halves (tests); by default the
    in-memory half uses naive UTC and the outbox age uses the DB's own clock
    (see `_db_clock`)."""
    age_clock: datetime | None = now
    now = now or _utcnow()
    state = state if state is not None else DELIVERY_HEALTH
    mem_peers, tick = state._snapshot()

    unavailable: str | None = None
    rows: list[tuple] = []
    if session is None:
        unavailable = "no session"
    else:
        try:
            rows = _outbox_rows(session)
        except Exception as exc:  # noqa: BLE001 - /health must never fail on this
            unavailable = format_error(exc)

    peers: dict[str, dict] = {}
    total = 0
    oldest_all: datetime | None = None
    for peer_source_id, target_url, n, oldest, attempts, db_now in rows:
        if age_clock is None and db_now is not None:
            age_clock = db_now
        key = peer_source_id or target_url
        mem = mem_peers.get(key, _PeerState())
        entry = peers.setdefault(
            key,
            {
                "pending": 0,
                "consecutive_failures": mem.consecutive_failures,
                "last_success_at": _iso(mem.last_success_at),
                "last_error": mem.last_error,
                "unreachable_since": _iso(mem.unreachable_since),
            },
        )
        n = int(n or 0)
        entry["pending"] += n
        if n:
            entry["consecutive_failures"] = max(
                entry["consecutive_failures"], int(attempts or 0)
            )
        total += n
        if oldest is not None and (oldest_all is None or oldest < oldest_all):
            oldest_all = oldest

    if unavailable is not None:
        # DB half missing: report what the loop itself has seen.
        for key, mem in mem_peers.items():
            peers[key] = {
                "pending": None,
                "consecutive_failures": mem.consecutive_failures,
                "last_success_at": _iso(mem.last_success_at),
                "last_error": mem.last_error,
                "unreachable_since": _iso(mem.unreachable_since),
            }

    outbox: dict = {
        "pending": total if unavailable is None else None,
        "oldest_pending_age_s": (
            round(max(((age_clock or now) - oldest_all).total_seconds(), 0.0), 1)
            if oldest_all is not None
            else None
        ),
        "peers": peers,
        "tick": {
            "last_at": _iso(tick.last_at),
            "last_error": tick.last_error,
            "consecutive_errors": tick.consecutive_errors,
        },
    }
    if unavailable is not None:
        outbox["unavailable"] = unavailable

    reasons: list[str] = []
    threshold = unreachable_after_seconds()
    for key in sorted(peers):
        since = mem_peers.get(key, _PeerState()).unreachable_since
        if since is None:
            continue
        down_for = (now - since).total_seconds()
        if down_for > threshold:
            err = peers[key]["last_error"]
            reasons.append(
                f"peer {key} not delivering for {int(down_for)}s "
                f"(> {int(threshold)}s)" + (f": {err}" if err else "")
            )
    if tick.consecutive_errors >= TICK_ERRORS_DEGRADE_AT:
        reasons.append(
            f"delivery tick raised {tick.consecutive_errors} consecutive times"
            + (f": {tick.last_error}" if tick.last_error else "")
        )

    return {
        "status": "degraded" if reasons else "ok",
        "degraded_reason": "; ".join(reasons) if reasons else None,
        "outbox": outbox,
    }


def replication_health_from_scope(session_scope, **kwargs) -> dict:
    """`replication_health` over a plugin's `session_scope()` context manager —
    the one-liner a plugin's `/health` calls (run it off the event loop, e.g.
    `anyio.to_thread.run_sync`). Opening the session failing is reported as
    `outbox.unavailable`, never raised."""
    try:
        with session_scope() as session:
            return replication_health(session, **kwargs)
    except Exception:  # noqa: BLE001 - /health must never fail on this
        # session_scope itself (or its commit) failed; answer from memory.
        return replication_health(None, **kwargs)


def apply_to_health(body: dict, block: dict) -> dict:
    """Fold a `replication_health` block into a plugin's `/health` body: sets
    `body["replication"]` (merging with any plugin-owned keys already there,
    e.g. pm's `dual_primary`), and lowers `body["status"]` to `degraded` with
    a top-level `degraded_reason` when the block degraded. Returns `body`."""
    rep = body.setdefault("replication", {})
    rep.update(block)
    if block.get("status") == "degraded":
        if body.get("status", "ok") == "ok":
            body["status"] = "degraded"
        reason = block.get("degraded_reason")
        if reason:
            prior = body.get("degraded_reason")
            body["degraded_reason"] = f"{prior}; {reason}" if prior else reason
    return body
