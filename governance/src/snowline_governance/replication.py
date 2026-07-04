"""Decision-event webhook bus — the EMIT side (governance-plugin spec §7).

Governance emits signed `decision.recorded` / `decision.superseded` events to
any registered subscriber (other plugins/services), so governance memory can
flow out without coupling governance to the consumer. Carried (functionality-
first, NOT imported) from the frozen monolith's `snowline_server.replication` —
the authoritative EMIT side (decision 97907576, #630):

  * `build_decision_event` — the payload shape (incl. `contract_version`),
  * `emit_decision_event` — the TRANSACTIONAL OUTBOX: a pending `WebhookDelivery`
    per matching subscription, written IN the decision's transaction,
  * `sign` — HMAC-SHA256 over the EXACT serialized body,
  * `deliver_pending` — the worker: per-subscription monotonic `seq` allocated
    at delivery time, retry, dead-letter at the attempt cap,
  * `webhook_delivery_loop` — the background timer (run in the app lifespan).

AMENDED for REPLICATION (replication-continuity spec §3.2, #77): the
delivery-time `seq` this module allocates — the recorded standing behavior of
decision 97907576 / #630 — is the right shape for fire-and-forget webhooks and
the WRONG one for replication streams (delivery order is not authoring order; a
re-created subscription restarts at 1). Replication-class subscriptions use the
SDK's `snowline_plugin_sdk.replication` emit module instead: `seq` allocated at
EMIT time in the domain write's transaction, streams keyed `(source_id, epoch)`,
`peer_seen` in the envelope, contract version 2. Governance adopts it in §9
item 3 (#79); THIS module remains the fire-and-forget bus until then.
Signatures stay DELIVERY-time over the exact bytes POSTed in both classes —
§5's hitless rotation depends on it.

IMPORT DISCIPLINE (import-purity, spec §10 / no cycle): this module imports from
governance models + stdlib + httpx/anyio/sqlalchemy ONLY — never the monolith,
substrate, the SDK, or `decisions.py`. The emit HOOK in `decisions.py` passes the
decision row + scope slug INTO `emit_decision_event`, so the dependency points
one way (decisions → replication), never the reverse. The published contract
constants come from `snowline_governance.contract` (a vendored copy, pinned EQUAL
to the SDK's by a drift-guard test).

THE ONE STRUCTURAL DELTA from the monolith: a subscription's optional `scope_id`
filter is a SOFT scope reference matched on the stable `scope_id` (no FK; scopes
are platform-owned) — `emit_decision_event` matches `s.scope_id == decision_row.
scope_id`, exactly as the monolith does, so the wire behavior is identical.

Subscription management (`create_subscription` / `list_subscriptions` /
`deactivate_subscription`) is PROGRAMMATIC — there is deliberately NO MCP tool or
CLI surface (remote subscription registration is out-of-band v1, per the SDK's
`events.py` note). SUPERSEDED for REPLICATION-CLASS subscriptions
(replication-continuity §5, #77): those are managed over the SDK's tailnet-gated
replication-admin surface (`snowline_plugin_sdk.replication.admin`) — still OFF
MCP. The no-surface posture stands for THIS module's fire-and-forget class.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import anyio
import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from snowline_governance.contract import CONTRACT_VERSION
from snowline_governance.db import session_scope
from snowline_governance.models import WebhookDelivery, WebhookSubscription

log = logging.getLogger("snowline.governance.replication")


def _source_id() -> str:
    """The emit-source identity stamped into every payload so a receiver can key a
    per-source watermark off it. Read live (not module-level) so a test/env change
    is honored. Defaults to "governance"."""
    return os.environ.get("SNOWLINE_REPLICATION_SOURCE_ID", "governance")


def _max_attempts() -> int:
    return int(os.environ.get("SNOWLINE_WEBHOOK_MAX_ATTEMPTS", "5"))


def _interval_seconds() -> int:
    return int(os.environ.get("SNOWLINE_WEBHOOK_INTERVAL", "30"))


# --- payload + outbox -------------------------------------------------------


def build_decision_event(event_type: str, decision_row, scope_slug: str) -> dict:
    """The webhook payload for one decision event. `decision_row` is the
    SQLAlchemy `Decision` instance just flushed; `scope_slug` is supplied by the
    caller (the emit hook) so this stays a pure shaping function with no DB
    lookups. `contract_version` is the published wire version a consumer checks;
    `source` is the per-source identity the receiver keys its watermark off."""
    return {
        "event_type": event_type,
        "source": _source_id(),
        "contract_version": CONTRACT_VERSION,
        "decision": {
            "id": str(decision_row.id),
            "scope": scope_slug,
            "decision": decision_row.decision,
            "rationale": decision_row.rationale,
            "recorded_at": (
                decision_row.recorded_at.isoformat()
                if decision_row.recorded_at
                else None
            ),
            "supersedes_id": (
                str(decision_row.supersedes_id)
                if decision_row.supersedes_id
                else None
            ),
        },
    }


def emit_decision_event(
    session: Session, event_type: str, decision_row, scope_slug: str
) -> None:
    """Write a pending `WebhookDelivery` row for EACH matching subscription, in
    the SAME transaction as the decision it carries (transactional outbox — the
    deliveries are atomic with `record_decision` / `supersede_decision`). A
    subscription matches when it is active, lists `event_type` in its
    `event_types`, and is either global (`scope_id` IS NULL) or anchored to the
    decision's scope (matched on the STABLE `scope_id`, mirroring the rest of
    governance's scope keying, #11).

    Deliberately allocates NO `seq` here — the row goes in with `seq=NULL` and the
    delivery loop assigns the per-subscription sequence at send time (see
    `deliver_pending`). Keeping `seq` allocation OUT of the decision transaction
    is the whole point: a `seq` collision must never be able to roll back the
    user's `record_decision`. So this hook only does an indexed read + an insert
    per subscriber — no `max()` aggregate, no flush-per-row on the hot path.

    When no subscription matches this is a near-zero-cost no-op (one cheap
    `active==True` read, then nothing) — `record_decision` pays almost nothing
    when nobody is subscribed (the common case)."""
    subs = session.scalars(
        select(WebhookSubscription).where(WebhookSubscription.active.is_(True))
    ).all()
    matching = [
        s
        for s in subs
        if event_type in (s.event_types or [])
        and (s.scope_id is None or s.scope_id == decision_row.scope_id)
    ]
    if not matching:
        return  # no subscribers → nothing to do (the common case)

    payload = build_decision_event(event_type, decision_row, scope_slug)
    for sub in matching:
        session.add(
            WebhookDelivery(
                subscription_id=sub.id,
                seq=None,  # allocated at delivery time by the loop
                event_type=event_type,
                payload=payload,
                status="pending",
            )
        )


# --- signing + delivery -----------------------------------------------------


def sign(secret: str, body: bytes) -> str:
    """HMAC-SHA256 of the raw request body under the subscription's shared
    secret, hex-encoded. Sent as `X-Snowline-Signature: sha256=<hexdigest>` so a
    receiver can verify authenticity + integrity. MUST be computed over the EXACT
    bytes POSTed (see `deliver_pending`), or the consumer's `verify_event`
    recomputation will not match."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def deliver_pending(session: Session, client: httpx.Client) -> int:
    """POST every still-deliverable webhook delivery (status pending or failed,
    under the attempt cap), in CREATION order, and record the outcome. Returns
    the count newly marked delivered.

    `seq` is allocated HERE (not at emit time): the first time a row is sent it
    gets the next per-subscription `max(seq)+1`. The delivery loop is the single
    writer of `seq` (one tick at a time), so that's race-free, and processing in
    `created_at` order means `seq` follows decision order — a supersession's row,
    created after the decision it supersedes, gets the higher `seq`. A retry keeps
    the `seq` already assigned (persisted on the first attempt).

    On a 2xx the row flips to `delivered` with `delivered_at` stamped; otherwise
    `attempts` increments, `last_error` is recorded, and the row flips to `failed`
    once it hits `MAX_ATTEMPTS` (else stays `pending` for a later tick). Commits
    PER ROW, so a mid-loop crash re-delivers at most the in-flight row (the
    receiver is idempotent by decision UUID) — progress genuinely survives."""
    max_attempts = _max_attempts()
    rows = session.scalars(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.status.in_(("pending", "failed")),
            WebhookDelivery.attempts < max_attempts,
        )
        .order_by(WebhookDelivery.created_at, WebhookDelivery.id)
    ).all()

    delivered = 0
    for row in rows:
        sub = session.get(WebhookSubscription, row.subscription_id)
        if sub is None:
            # Subscription hard-deleted out from under a queued delivery (the FK
            # CASCADE normally prevents this). Terminalize so it can't spin
            # forever on every tick.
            row.status = "failed"
            row.last_error = "subscription no longer exists"
            session.commit()
            continue
        # Assign the per-subscription seq on the first send attempt; keep it on
        # retries (already persisted).
        if row.seq is None:
            row.seq = (
                session.scalar(
                    select(func.coalesce(func.max(WebhookDelivery.seq), 0)).where(
                        WebhookDelivery.subscription_id == row.subscription_id
                    )
                )
            ) + 1
        # Serialize ONCE so the signature covers exactly the bytes we POST (a
        # re-serialization by httpx's `json=` could differ in separators/ordering
        # and break verification on the receiver). The seq is merged into the
        # signed body so a receiver reading the body alone gets the ordering key.
        body = json.dumps({**row.payload, "seq": row.seq}).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Snowline-Event": row.event_type,
            "X-Snowline-Delivery-Seq": str(row.seq),
            "X-Snowline-Signature": f"sha256={sign(sub.secret, body)}",
        }
        try:
            resp = client.post(sub.target_url, content=body, headers=headers)
            if 200 <= resp.status_code < 300:
                row.status = "delivered"
                row.delivered_at = datetime.now(timezone.utc)
                delivered += 1
            else:
                _record_failure(row, max_attempts, f"HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001 - any transport error is a retry
            _record_failure(row, max_attempts, str(exc))
        session.commit()  # per-row: the outcome (incl. the assigned seq) is durable
    return delivered


def _record_failure(row: WebhookDelivery, max_attempts: int, error: str) -> None:
    row.attempts = (row.attempts or 0) + 1
    row.last_error = error
    row.status = "failed" if row.attempts >= max_attempts else "pending"


async def webhook_delivery_loop() -> None:
    """Background loop that drains pending webhook deliveries on a timer
    (`SNOWLINE_WEBHOOK_INTERVAL`, default 30s). Mirrors the monolith's delivery
    loop: one `session_scope()` + one `httpx.Client` per tick, per-tick
    exceptions swallowed/logged so a single bad delivery (or a transient receiver
    outage) can never kill the loop.

    Disable with `SNOWLINE_WEBHOOK_DISABLED` (parallels the monolith's flag)."""
    if os.environ.get("SNOWLINE_WEBHOOK_DISABLED"):
        log.info("webhook delivery disabled via SNOWLINE_WEBHOOK_DISABLED")
        return
    while True:
        try:
            await anyio.to_thread.run_sync(_deliver_once)
        except Exception:
            log.exception("webhook delivery tick failed; loop continues")
        await anyio.sleep(_interval_seconds())


def _deliver_once() -> None:
    """One delivery tick: open a session + client, drain pending deliveries. Runs
    off the event loop (httpx.Client is sync) via `to_thread`."""
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        with session_scope() as session:
            n = deliver_pending(session, client)
    if n:
        log.info("webhook delivery: delivered=%d", n)


# --- subscription management (programmatic API; no MCP tool / no CLI) --------


def create_subscription(
    session: Session,
    target_url: str,
    secret: str,
    event_types: list[str],
    scope_id: uuid.UUID | str | None = None,
) -> dict:
    """Register a webhook subscriber. `scope_id` is an optional SOFT scope
    reference (the stable platform scope id) restricting matches to that one
    scope; omit/None for a GLOBAL subscription that matches every decision.
    Programmatic only — there is no MCP/CLI surface (remote registration is
    out-of-band v1)."""
    sid = None
    if scope_id is not None:
        sid = (
            scope_id
            if isinstance(scope_id, uuid.UUID)
            else uuid.UUID(str(scope_id))
        )
    sub = WebhookSubscription(
        target_url=target_url,
        secret=secret,
        event_types=list(event_types),
        scope_id=sid,
        active=True,
    )
    session.add(sub)
    session.flush()
    return _subscription_dict(sub)


def list_subscriptions(session: Session) -> list[dict]:
    """Every registered subscription (active and inactive), newest first."""
    rows = session.scalars(
        select(WebhookSubscription).order_by(
            WebhookSubscription.created_at.desc(), WebhookSubscription.id
        )
    ).all()
    return [_subscription_dict(s) for s in rows]


def deactivate_subscription(session: Session, subscription_id: str) -> dict:
    """Flip a subscription inactive so it stops matching (a soft delete that keeps
    its delivery log). Raises ValueError on an unknown id."""
    key = (
        subscription_id
        if isinstance(subscription_id, uuid.UUID)
        else uuid.UUID(str(subscription_id))
    )
    sub = session.get(WebhookSubscription, key)
    if sub is None:
        raise ValueError(f"no webhook subscription with id {subscription_id!r}")
    sub.active = False
    session.flush()
    return _subscription_dict(sub)


def _subscription_dict(sub: WebhookSubscription) -> dict:
    return {
        "id": str(sub.id),
        "target_url": sub.target_url,
        "event_types": list(sub.event_types or []),
        "scope_id": str(sub.scope_id) if sub.scope_id else None,
        "active": sub.active,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
    }
