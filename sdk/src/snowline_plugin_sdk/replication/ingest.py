"""The INGEST half — per-stream watermark + contiguous apply, signature verify,
origin suppression, parking (replication-continuity §3.2/§5/§8.1, issue #77).

Carved from the frozen monolith's `snowline_server.replication_ingest`
(read-only reference, governance-plugin §9) with the §3.2 stream contract in
place of the monolith's per-source inbox buffer:

  * A stream is `(source_id, epoch)`; the watermark is keyed per stream, so a
    re-pair's fresh epoch restarting at seq 1 can never collide with the old
    epoch's watermark.
  * CONTIGUOUS APPLY replaces buffering: the receiver applies exactly
    `gate_seq + 1` and REFUSES an out-of-order delivery with "expected seq N"
    (the sender's per-stream cursor never advances past an undelivered seq, so
    in-order redelivery is the sender's job, not a receiver-side inbox's).
  * Two counters, forced apart by parking (§3.2): `gate_seq` — the DELIVERY
    gate, which parking advances so the stream flows — and `applied_seq` — the
    contiguous APPLIED frontier that `peer_seen` reports. A parked seq PINS
    `applied_seq` even as later seqs apply past it; re-applying from the park
    unpins it. (A max-style counter would let a parked-unseen event masquerade
    as seen, silently blinding §6.1's concurrency detection.)

The plugin supplies the domain APPLY function (envelope in, idempotent local
write out — §4 checklist item 4: semantic idempotence, e.g. INSERT … ON
CONFLICT DO NOTHING on the event's UUID). The SDK runs it inside the ingest
transaction UNDER ORIGIN SUPPRESSION (the emit hook is disabled — §3.2's hard
rule: an ingest-applied write never re-emits), and every apply exception is a
bounded retryable error (§8.1): the sender redelivers under backoff until the
parking bound, then the event parks LOUDLY — the park ACKs to the sender, the
gate advances, and the parked event stays re-appliable once the cause is fixed.

The one opt-out is `ParkNow` (issue #92): an apply that KNOWS a failure is
permanent (a cross-partition slug collision that will never stop colliding)
raises it to park the event on THIS delivery, skipping a retry budget that
would only add latency before the inevitable park. A `ParkNow` park is
bit-for-bit the same first-class §8.1 state a bound-reached park produces —
same parked row, same ACK, same gate advance, same `applied_seq` pin,
re-appliable the same way (both go through the one `_park` helper) — it only
skips the pointless wait. A merely SLOW-to-heal error (an unknown parent slug
still replicating) must NOT raise it: that is what the bound's retries are for.

Version skew on a live stream is a HOLD, not a failure (§3.2): a version-AHEAD
envelope (peer upgraded first) and a v1 envelope on a v2-paired stream are both
409 retryable refusals — never accept-and-misprocess (`check_contract_version`'s
<= rule is for consumers of a stable envelope, not for a stream whose keying
fields changed). Rejection (400/401/404 → the sender dead-letters) is reserved
for envelopes invalid under every version either side has spoken.
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from snowline_plugin_sdk.contract import CONTRACT_VERSION
from snowline_plugin_sdk.replication.envelope import (
    REFUSAL_OUT_OF_ORDER,
    REFUSAL_VERSION_HOLD,
    REJECT_BAD_SIGNATURE,
    REJECT_MALFORMED,
    REJECT_UNKNOWN_STREAM,
    RETRY_APPLY_FAILED,
    STATUS_APPLIED,
    STATUS_DUPLICATE,
    STATUS_PARKED,
    STREAM_FIELDS,
    verify_signature,
)
from snowline_plugin_sdk.replication.models import (
    ReplicationInboundStream,
    ReplicationParkedEvent,
)

log = logging.getLogger("snowline_plugin_sdk.replication.ingest")


class ParkNow(Exception):
    """Raised by a plugin apply function to opt one event into IMMEDIATE parking,
    skipping the §8.1 retry budget — the "this will never self-heal" fast path
    (issue #92).

    The default apply contract is uniformly retryable (§8.1): every OTHER apply
    exception is redelivered under backoff until the one shared bound, then
    parks. An apply raises `ParkNow` ONLY when it KNOWS the failure is permanent
    (e.g. a cross-partition slug collision that will never stop colliding), where
    burning the full backoff budget before the inevitable park is pure latency.

    The resulting park is bit-for-bit the SAME surfaced state a bound-reached
    park produces — same parked row (stream/seq/event_type/payload/reason/
    parked_at), same 200 `parked` ACK to the sender, same gate advance, same
    `applied_seq` pin, re-appliable via `reapply_parked` the same way (both
    paths go through the one `_park` helper). The ONLY differences are
    deterministic and documented: the parked row's `reason` is this exception's
    message (`str(exc)` — the identical plumbing a bound-reached park uses for
    the final failure's `str(exc)`), and the emitted log line names the
    fast-path. There is no attempt-count field on a parked row to reconcile.

    A merely slow-to-heal error (an unknown parent slug still replicating) must
    NOT raise `ParkNow`: it needs the bound's retries to self-heal."""


def _park_after_attempts() -> int:
    """The §8.1 parking bound: consecutive retryable apply failures at the gate
    before the event parks. Default 60 ≈ five hours at the sender's default
    cadence (backoff ramps 30s → the 5-minute cap) — generous against any real
    scope-stream lag, bounded against a poison event freezing its stream."""
    return int(os.environ.get("SNOWLINE_REPLICATION_PARK_AFTER_ATTEMPTS", "60"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- origin suppression (§3.2, hard rule) -------------------------------------

_APPLYING: ContextVar[bool] = ContextVar("snowline_replication_applying", default=False)


def is_applying_replicated_event() -> bool:
    """True while the SDK ingest path is running a plugin apply function. The
    SDK's own `emit_event` checks this and no-ops (events exist only for
    locally-originated writes); a plugin whose emit hook does NOT go through
    the SDK emit module must check it itself — the §3.2 hard rule, or a
    delivered event boomerangs between the pair forever."""
    return _APPLYING.get()


@contextmanager
def _applying_replicated_event():
    token = _APPLYING.set(True)
    try:
        yield
    finally:
        _APPLYING.reset(token)


# --- inbound stream registration + rotation (§5) ------------------------------


def register_inbound_stream(
    session: Session, source_id: str, epoch: str, *, secret: str | None = None
) -> dict:
    """Register one inbound stream `(source_id, epoch)` — the RECEIVING half of
    the §5 handshake. The receiver MINTS the secret (a secret only the sender
    knows can never verify) and this returns it ONCE, over the tailnet; it is
    never listed again and never logged. `secret` may be supplied only by the
    §7 seed script, which plays the receiver's part before the spoke's store
    exists. Re-registering a live stream raises (rotation is `rotate_inbound_secret`,
    re-pairing mints a fresh epoch)."""
    if session.get(ReplicationInboundStream, (source_id, epoch)) is not None:
        raise ValueError(f"inbound stream ({source_id!r}, {epoch!r}) already exists")
    row = ReplicationInboundStream(
        source_id=source_id,
        epoch=epoch,
        secret=secret or _secrets.token_hex(32),
        active=True,
    )
    session.add(row)
    session.flush()
    return {**_stream_dict(row), "secret": row.secret}


def list_inbound_streams(session: Session) -> list[dict]:
    """Every registered inbound stream (active and retired), newest first —
    watermark state included, secrets NEVER (returned once at registration)."""
    rows = session.scalars(
        select(ReplicationInboundStream).order_by(
            ReplicationInboundStream.created_at.desc(),
            ReplicationInboundStream.source_id,
        )
    ).all()
    return [_stream_dict(r) for r in rows]


def retire_inbound_stream(session: Session, source_id: str, epoch: str) -> dict:
    """Soft-retire an inbound stream (re-pair/re-seed, §7 step 5): deliveries on
    it are refused as unknown from now on; the watermark row stays for audit."""
    row = session.get(ReplicationInboundStream, (source_id, epoch))
    if row is None:
        raise ValueError(f"no inbound stream ({source_id!r}, {epoch!r})")
    row.active = False
    row.retired_at = _utcnow()
    session.flush()
    return _stream_dict(row)


def rotate_inbound_secret(session: Session, source_id: str, epoch: str) -> dict:
    """§5 rotation, receiver side: mint a replacement secret for a LIVE stream —
    no epoch change, no re-seed. Old and new both verify during the switch; the
    old retires on the first new-signed delivery (`ingest_delivery`). Returns
    the new secret ONCE (the pairing CLI carries it to the sender).

    Re-rotating while a rotation is still pending (the sender hasn't swapped)
    KEEPS `previous_secret` — the one secret the sender provably still signs
    with — and discards only the never-delivered replacement. Overwriting it
    with the undelivered mint would strand the sender at the original secret →
    401s (the double-rotate race: a pairing CLI that crashed between mint and
    carry gets re-run, and the stream must survive that re-run). The
    alternative — refusing while pending — would make that same crash
    permanent: the lost mint could never be re-issued and retirement (which
    needs a new-signed delivery) could never happen."""
    row = session.get(ReplicationInboundStream, (source_id, epoch))
    if row is None or not row.active:
        raise ValueError(f"no active inbound stream ({source_id!r}, {epoch!r})")
    if row.previous_secret is None:
        row.previous_secret = row.secret
    row.secret = _secrets.token_hex(32)
    session.flush()
    return {**_stream_dict(row), "secret": row.secret}


def _stream_dict(row: ReplicationInboundStream) -> dict:
    return {
        "source_id": row.source_id,
        "epoch": row.epoch,
        "gate_seq": row.gate_seq,
        "applied_seq": row.applied_seq,
        "blocked_seq": row.blocked_seq,
        "blocked_attempts": row.blocked_attempts,
        "rotation_pending": row.previous_secret is not None,
        "active": row.active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "retired_at": row.retired_at.isoformat() if row.retired_at else None,
    }


# --- the delivery gate + contiguous apply (§3.2) -------------------------------


def ingest_delivery(
    session: Session,
    body: bytes,
    signature: str | None,
    apply,
    *,
    park_after: int | None = None,
) -> tuple[int, dict]:
    """Accept ONE signed delivery: verify, gate, apply. Returns
    `(http_status, response_body)` — transport-agnostic so `admin.py`'s route is
    a thin shim and tests need no HTTP. `apply` is the plugin's idempotent
    domain apply: `apply(session, envelope) -> None`, run in THIS session's
    transaction under origin suppression.

    Outcome map (the envelope.py vocabulary; the sender classifies by status):
      200 `applied`    — seq was exactly `gate_seq + 1` and apply succeeded.
      200 `duplicate`  — seq at/under the gate: ACKed as a no-op (§8.1 — the
                         gate, not `applied_seq`, keys the dedupe: a redelivery
                         of a PARKED seq must not re-apply out of order).
      200 `parked`     — apply kept failing past the bound (§8.1) OR raised
                         `ParkNow` (#92: a permanent failure, parked on this
                         delivery with no retry budget spent): the event moved
                         whole into the park, the gate advanced, and this ACKs
                         so the sender's cursor moves on. `applied_seq` did NOT
                         advance. Both routes produce identical park state.
      409 `out_of_order` / `version_hold` — retryable refusals (§3.1/§3.2).
      503 `apply_failed` — a retryable apply error under the bound; the sender
                         backs off and redelivers.
      400/401/404      — rejections (malformed / bad signature / unknown
                         stream): the sender dead-letters.

    TRANSACTION CONTRACT: run ONE delivery per transaction (the admin route's
    per-request `session_scope` does). A failed apply ROLLS the session BACK to
    its last commit so the §8.1 failure counting commits on a clean
    transaction — the apply seam promises idempotence, not atomicity, and a
    partial apply must not leak out under the counting state's commit.
    """
    import json

    try:
        envelope = json.loads(body)
        if not isinstance(envelope, dict):
            raise ValueError("envelope must be a JSON object")
    except Exception:  # noqa: BLE001 - any parse failure is the same rejection
        return 400, {"status": "rejected", "reason": REJECT_MALFORMED}

    source_id = envelope.get("source")
    epoch = envelope.get("epoch")
    version = envelope.get("contract_version")

    # Locate the stream FIRST (signature verification needs its secret). An
    # envelope that can't name a registered stream may still be a SKEWED peer's
    # shape — a v1 peer (no `epoch` yet) or a future contract whose keying
    # fields we can't parse. Both directions hold retryably (§3.2: the backlog
    # waits out the rolling upgrade, never misprocessed, never dead-lettered);
    # only an envelope claiming OUR version while missing our keying fields is
    # malformed under every version either side has spoken.
    if not isinstance(source_id, str) or not isinstance(epoch, str):
        if isinstance(version, int) and version >= 1 and version != CONTRACT_VERSION:
            return 409, {"status": "refused", "reason": REFUSAL_VERSION_HOLD}
        return 400, {"status": "rejected", "reason": REJECT_MALFORMED}

    stream = session.get(ReplicationInboundStream, (source_id, epoch))
    if stream is None or not stream.active:
        return 404, {"status": "rejected", "reason": REJECT_UNKNOWN_STREAM}

    # Verify against the current secret, then the pre-rotation one (§5: both
    # accepted during the switch). The FIRST new-signed delivery retires the
    # old secret — delivery-time signing guarantees the sender's whole backlog
    # re-signs once it swaps, so retirement can't strand queued rows.
    if verify_signature(stream.secret, body, signature):
        if stream.previous_secret is not None:
            stream.previous_secret = None
            session.flush()
    elif not verify_signature(stream.previous_secret or "", body, signature) or (
        stream.previous_secret is None
    ):
        return 401, {"status": "rejected", "reason": REJECT_BAD_SIGNATURE}

    # Version skew on a live stream is a HOLD, not a failure (§3.2) — in BOTH
    # directions: version-AHEAD (the peer upgraded first; wait out ours) and
    # version-BEHIND (a v1 envelope on a v2-paired stream; never
    # accept-and-misprocess under the old <= rule).
    if not isinstance(version, int) or version < 1:
        return 400, {"status": "rejected", "reason": REJECT_MALFORMED}
    if version != CONTRACT_VERSION:
        return 409, {"status": "refused", "reason": REFUSAL_VERSION_HOLD}

    seq = envelope.get("seq")
    peer_seen = envelope.get("peer_seen")
    # peer_seen is validated as strictly as seq: it flows raw into the plugin
    # apply seam, and §6.1's concurrency detection consumes it — a garbage
    # value silently corrupting that comparison is worse than a loud 400.
    if (
        not isinstance(seq, int)
        or isinstance(seq, bool)  # JSON true parses as a Python int — exclude
        or seq < 1
        or not isinstance(peer_seen, int)
        or isinstance(peer_seen, bool)
        or peer_seen < 0
        or any(f not in envelope for f in STREAM_FIELDS)
    ):
        return 400, {"status": "rejected", "reason": REJECT_MALFORMED}

    # The delivery gate (§3.2): at/under → duplicate no-op ACK; past the next
    # slot → ordering refusal with the expected seq; exactly gate+1 → apply.
    if seq <= stream.gate_seq:
        return 200, {"status": STATUS_DUPLICATE, "gate_seq": stream.gate_seq}
    if seq > stream.gate_seq + 1:
        return 409, {
            "status": "refused",
            "reason": REFUSAL_OUT_OF_ORDER,
            "expected_seq": stream.gate_seq + 1,
        }

    try:
        with _applying_replicated_event():
            apply(session, envelope)
    except ParkNow as exc:  # the apply declared the failure permanent (#92)
        return _park_now(session, stream, envelope, exc)
    except Exception as exc:  # noqa: BLE001 - every other apply error is §8.1-retryable
        return _apply_failed(session, stream, envelope, exc, park_after)

    stream.gate_seq = seq
    stream.blocked_seq = None
    stream.blocked_attempts = 0
    _recompute_applied_seq(session, stream)
    session.flush()
    return 200, {
        "status": STATUS_APPLIED,
        "gate_seq": stream.gate_seq,
        "applied_seq": stream.applied_seq,
    }


def _apply_failed(
    session: Session,
    stream: ReplicationInboundStream,
    envelope: dict,
    exc: Exception,
    park_after: int | None,
) -> tuple[int, dict]:
    """A retryable apply error at the gate (§8.1): count it toward the parking
    bound; under the bound the sender redelivers (503), at the bound the event
    parks — loudly — the gate advances so the stream flows, and the park ACKs.

    The failed apply's partial writes are rolled back so the counting/parking
    state commits on a clean transaction (the apply seam promises idempotence,
    not atomicity)."""
    session.rollback()
    seq = envelope["seq"]
    if stream.blocked_seq == seq:
        stream.blocked_attempts += 1
    else:
        stream.blocked_seq = seq
        stream.blocked_attempts = 1
    bound = park_after if park_after is not None else _park_after_attempts()
    if stream.blocked_attempts < bound:
        session.flush()
        return 503, {
            "status": "retry",
            "reason": RETRY_APPLY_FAILED,
            "error": str(exc),
            "attempts": stream.blocked_attempts,
        }
    log.error(
        "replication event PARKED (stream %s/%s seq %s) after %s failed applies: %s",
        stream.source_id, stream.epoch, seq, bound, exc,
    )
    return _park(session, stream, envelope, str(exc))


def _park_now(
    session: Session,
    stream: ReplicationInboundStream,
    envelope: dict,
    exc: ParkNow,
) -> tuple[int, dict]:
    """The `ParkNow` fast path (§8.1, issue #92): the apply declared this
    failure permanent, so the event parks on THIS delivery with NO retry budget
    spent. Same rollback discipline as `_apply_failed` (a partial apply must not
    leak out under the park commit — the apply seam promises idempotence, not
    atomicity) and the SAME `_park` state as a bound-reached park; only the retry
    counting is skipped. `blocked_seq`/`blocked_attempts` land at `(None, 0)` via
    `_park` whether or not this seq accrued earlier transient failures — the same
    reset a bound-reached park performs."""
    session.rollback()
    log.error(
        "replication event PARKED NOW (stream %s/%s seq %s) — apply declared the "
        "failure permanent, no retry budget spent: %s",
        stream.source_id, stream.epoch, envelope["seq"], exc,
    )
    return _park(session, stream, envelope, str(exc))


def _park(
    session: Session,
    stream: ReplicationInboundStream,
    envelope: dict,
    reason: str,
) -> tuple[int, dict]:
    """Move one event WHOLE into the park (§8.1) and advance the gate past it,
    pinning `applied_seq`. The SINGLE park code path: a bound-reached park
    (`_apply_failed`) and a `ParkNow` fast-path park (`_park_now`) both funnel
    through here, so their surfaced state is bit-for-bit identical — same parked
    row fields, same gate advance, same `blocked_*` reset, same frontier pin,
    same 200 `parked` ACK. The only per-caller difference is the `reason` string
    (each caller passes `str(exc)`) and the log line each emits before calling."""
    seq = envelope["seq"]
    session.add(
        ReplicationParkedEvent(
            source_id=stream.source_id,
            epoch=stream.epoch,
            seq=seq,
            event_type=envelope.get("event_type", ""),
            payload=envelope,
            reason=reason,
        )
    )
    stream.gate_seq = seq  # the gate advances past the park — the stream flows
    stream.blocked_seq = None
    stream.blocked_attempts = 0
    _recompute_applied_seq(session, stream)  # …but the applied frontier PINS
    session.flush()
    return 200, {
        "status": STATUS_PARKED,
        "gate_seq": stream.gate_seq,
        "applied_seq": stream.applied_seq,
    }


def _recompute_applied_seq(
    session: Session, stream: ReplicationInboundStream
) -> None:
    """`applied_seq` = the contiguous APPLIED frontier (§3.2). Every seq at or
    under the gate was either applied or parked (the gate only advances on one
    of the two), so the frontier is derivable: the lowest parked seq minus one,
    or the gate itself when nothing is parked. Recomputing (rather than
    incrementing) makes park-pinning and re-apply-unpinning fall out of one
    rule."""
    parked = session.scalars(
        select(ReplicationParkedEvent.seq)
        .where(
            ReplicationParkedEvent.source_id == stream.source_id,
            ReplicationParkedEvent.epoch == stream.epoch,
        )
        .order_by(ReplicationParkedEvent.seq)
    ).first()
    stream.applied_seq = (parked - 1) if parked is not None else stream.gate_seq


# --- parking: surface + re-apply (§8.1) ----------------------------------------


def list_parked(session: Session) -> list[dict]:
    """Every parked event, oldest first — the §8.1 loud-first-class-state read
    (tool + UI widget + health signal feed off this; an empty list is the
    standing invariant to watch)."""
    rows = session.scalars(
        select(ReplicationParkedEvent).order_by(
            ReplicationParkedEvent.parked_at, ReplicationParkedEvent.seq
        )
    ).all()
    return [
        {
            "source_id": r.source_id,
            "epoch": r.epoch,
            "seq": r.seq,
            "event_type": r.event_type,
            "payload": r.payload,
            "reason": r.reason,
            "parked_at": r.parked_at.isoformat() if r.parked_at else None,
        }
        for r in rows
    ]


def reapply_parked(
    session: Session, source_id: str, epoch: str, seq: int, apply
) -> dict:
    """Re-apply one parked event after its cause is fixed (§8.1) — apply
    idempotence makes the replay safe. On success the parked row is removed and
    the frontier UNPINS: `applied_seq` advances through the formerly-parked seq
    and any contiguously-applied span beyond it (§3.2). On failure the event
    stays parked (the error propagates — re-apply is an operator action, not a
    retry loop)."""
    parked = session.get(ReplicationParkedEvent, (source_id, epoch, seq))
    if parked is None:
        raise ValueError(f"no parked event ({source_id!r}, {epoch!r}, seq {seq})")
    stream = session.get(ReplicationInboundStream, (source_id, epoch))
    if stream is None:
        raise ValueError(f"no inbound stream ({source_id!r}, {epoch!r})")
    with _applying_replicated_event():
        apply(session, parked.payload)
    session.delete(parked)
    session.flush()
    _recompute_applied_seq(session, stream)
    session.flush()
    return _stream_dict(stream)
