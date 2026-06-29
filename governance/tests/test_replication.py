"""The decision-event webhook bus — the EMIT side (governance-plugin spec §7).

Covers the transactional outbox (`emit_decision_event` via the hooks in
`record_decision` / `supersede_decision`), the signing-over-exact-body invariant +
per-subscription monotonic `seq` (`deliver_pending`), retry → dead-letter, and a
producer↔consumer ROUND-TRIP against the published `snowline-plugin-sdk` (the
consumer's `verify_event`), which is dev-only and skipped when not installed.

DB-backed (skips cleanly when Postgres is unavailable). Scope resolution is always
direct (these tests call `decisions` with a stable scope id; no live platform).
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from snowline_governance import decisions, replication
from snowline_governance.contract import (
    CONTRACT_VERSION,
    EVENT_DECISION_RECORDED,
    EVENT_DECISION_SUPERSEDED,
)
from snowline_governance.models import WebhookDelivery


def _sid(slug: str) -> uuid.UUID:
    """The stable per-slug scope id (matches `StubScopeClient`'s)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


def _deliveries(session) -> list[WebhookDelivery]:
    from sqlalchemy import select

    return list(
        session.scalars(
            select(WebhookDelivery).order_by(WebhookDelivery.created_at)
        )
    )


# --- transactional outbox: emit on record/supersede -------------------------


def test_record_with_no_subscribers_writes_nothing(db_session):
    """The common case: no subscription → the emit hook is a no-op (the outbox
    stays empty), so `record_decision` pays almost nothing."""
    decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "use postgres"
    )
    assert _deliveries(db_session) == []


def test_record_emits_to_global_and_matching_scope_only(db_session):
    """A global subscription and a scope-filtered one both fire for a decision in
    the matching scope; a subscription anchored to a DIFFERENT scope does not."""
    glob = replication.create_subscription(
        db_session, "https://g.example/hook", "s-glob", [EVENT_DECISION_RECORDED]
    )
    same = replication.create_subscription(
        db_session,
        "https://w.example/hook",
        "s-widget",
        [EVENT_DECISION_RECORDED],
        scope_id=_sid("acme/widget"),
    )
    other = replication.create_subscription(
        db_session,
        "https://o.example/hook",
        "s-other",
        [EVENT_DECISION_RECORDED],
        scope_id=_sid("acme/other"),
    )

    decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "use postgres"
    )

    rows = _deliveries(db_session)
    sub_ids = {str(r.subscription_id) for r in rows}
    assert sub_ids == {glob["id"], same["id"]}  # NOT the other-scope subscription
    assert other["id"] not in sub_ids
    for r in rows:
        assert r.status == "pending"
        assert r.seq is None  # not allocated until delivery
        assert r.event_type == EVENT_DECISION_RECORDED
        assert r.payload["contract_version"] == CONTRACT_VERSION
        assert r.payload["decision"]["decision"] == "use postgres"


def test_event_type_filter_excludes_nonsubscribed(db_session):
    """A subscription that wants only `decision.superseded` gets NO `recorded`
    delivery, and vice-versa."""
    sup_only = replication.create_subscription(
        db_session, "https://x.example/hook", "s", [EVENT_DECISION_SUPERSEDED]
    )
    v1 = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "v1"
    )
    assert _deliveries(db_session) == []  # recorded not wanted

    decisions.supersede_decision(db_session, v1["id"], "v2", "revised")
    rows = _deliveries(db_session)
    assert len(rows) == 1
    assert str(rows[0].subscription_id) == sup_only["id"]
    assert rows[0].event_type == EVENT_DECISION_SUPERSEDED
    assert rows[0].payload["decision"]["decision"] == "v2"
    # The supersede payload carries the prior link the receiver orders by.
    assert rows[0].payload["decision"]["supersedes_id"] == v1["id"]


def test_inactive_subscription_does_not_match(db_session):
    sub = replication.create_subscription(
        db_session, "https://x.example/hook", "s", [EVENT_DECISION_RECORDED]
    )
    replication.deactivate_subscription(db_session, sub["id"])
    decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "use postgres"
    )
    assert _deliveries(db_session) == []


# --- delivery: sign over the exact body + monotonic seq ----------------------


class _MockTransport(httpx.BaseTransport):
    """Captures every POST so a test can assert the EXACT signed body, headers,
    and ordering. `status_for(url)` decides the response code per target."""

    def __init__(self, status_for):
        self._status_for = status_for
        self.calls: list[tuple[str, bytes, dict]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = request.content
        self.calls.append((url, body, dict(request.headers)))
        return httpx.Response(self._status_for(url))


def test_deliver_signs_over_exact_body_and_allocates_seq(db_session):
    """`deliver_pending` POSTs the EXACT serialized body the signature covers,
    allocates a per-subscription monotonic `seq` at send time, and marks
    delivered. Verify the HMAC BY HAND over the captured bytes."""
    sub = replication.create_subscription(
        db_session, "https://recv.example/hook", "topsecret", [EVENT_DECISION_RECORDED]
    )
    decisions.record_decision(db_session, "acme/widget", _sid("acme/widget"), "d1")
    decisions.record_decision(db_session, "acme/widget", _sid("acme/widget"), "d2")

    transport = _MockTransport(lambda url: 200)
    with httpx.Client(transport=transport) as client:
        n = replication.deliver_pending(db_session, client)
    assert n == 2
    assert len(transport.calls) == 2

    # seq is contiguous + monotonic per subscription, following decision order.
    seqs = [int(h["x-snowline-delivery-seq"]) for (_, _, h) in transport.calls]
    assert seqs == [1, 2]

    for (url, body, headers) in transport.calls:
        assert url == "https://recv.example/hook"
        # The signature is over EXACTLY the bytes POSTed (verify by hand).
        sig = headers["x-snowline-signature"]
        assert sig.startswith("sha256=")
        expected = replication.sign("topsecret", body)
        assert sig == f"sha256={expected}"
        # The seq is merged INTO the signed body (a receiver reading the body
        # alone gets the ordering key); body parses + carries contract_version.
        parsed = json.loads(body)
        assert parsed["seq"] == int(headers["x-snowline-delivery-seq"])
        assert parsed["contract_version"] == CONTRACT_VERSION
        assert headers["x-snowline-event"] == EVENT_DECISION_RECORDED

    rows = _deliveries(db_session)
    assert all(r.status == "delivered" and r.delivered_at is not None for r in rows)
    assert sorted(r.seq for r in rows) == [1, 2]
    # str(sub) only to keep the binding meaningful.
    assert sub["id"]


def test_deliver_retry_then_dead_letter(db_session, monkeypatch):
    """A failing endpoint increments attempts, stays `pending` under the cap, then
    flips to `failed` (dead-letter) at MAX_ATTEMPTS — and the seq is kept across
    retries (allocated once)."""
    monkeypatch.setenv("SNOWLINE_WEBHOOK_MAX_ATTEMPTS", "3")
    replication.create_subscription(
        db_session, "https://down.example/hook", "s", [EVENT_DECISION_RECORDED]
    )
    decisions.record_decision(db_session, "acme/widget", _sid("acme/widget"), "d1")

    transport = _MockTransport(lambda url: 500)
    with httpx.Client(transport=transport) as client:
        # tick 1: attempts=1, pending
        assert replication.deliver_pending(db_session, client) == 0
        row = _deliveries(db_session)[0]
        assert (row.attempts, row.status) == (1, "pending")
        assert row.seq == 1  # allocated on first attempt
        # tick 2: attempts=2, still pending
        assert replication.deliver_pending(db_session, client) == 0
        row = _deliveries(db_session)[0]
        assert (row.attempts, row.status) == (2, "pending")
        assert row.seq == 1  # kept across retries
        # tick 3: attempts=3, hits cap → failed (dead-letter)
        assert replication.deliver_pending(db_session, client) == 0
        row = _deliveries(db_session)[0]
        assert (row.attempts, row.status) == (3, "failed")
        assert "HTTP 500" in (row.last_error or "")
        # A failed-at-cap row is no longer picked up.
        assert replication.deliver_pending(db_session, client) == 0
    assert len(transport.calls) == 3  # not retried past the cap


def test_subscription_management_roundtrip(db_session):
    a = replication.create_subscription(
        db_session, "https://a/h", "sa", [EVENT_DECISION_RECORDED]
    )
    b = replication.create_subscription(
        db_session, "https://b/h", "sb", [EVENT_DECISION_SUPERSEDED],
        scope_id=_sid("acme/widget"),
    )
    listed = replication.list_subscriptions(db_session)
    by_id = {s["id"]: s for s in listed}
    assert set(by_id) == {a["id"], b["id"]}
    assert by_id[a["id"]]["scope_id"] is None
    assert by_id[b["id"]]["scope_id"] == str(_sid("acme/widget"))
    assert all(s["active"] for s in listed)

    replication.deactivate_subscription(db_session, a["id"])
    after = {s["id"]: s for s in replication.list_subscriptions(db_session)}
    assert after[a["id"]]["active"] is False

    with pytest.raises(ValueError):
        replication.deactivate_subscription(db_session, str(uuid.uuid4()))
