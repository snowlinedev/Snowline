"""The tailnet-gated replication HTTP surface (replication-continuity §5,
issue #77): the ingest route + the replication-admin routes, exercised through
a real FastAPI app over httpx's ASGI transport (no server; the transport's
`client=` sets the peer IP the trust gate sees).
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import anyio
import httpx
import pytest
from fastapi import FastAPI

from snowline_plugin_sdk.replication.admin import build_replication_router
from snowline_plugin_sdk.replication.envelope import build_envelope, sign_body

TAILNET_PEER = "100.64.0.7"
HOTEL_LAN_PEER = "203.0.113.9"


@pytest.fixture()
def app_and_applied(make_instance):
    """A plugin-shaped app: the SDK router over a fresh store, with a recording
    apply seam."""
    sessions = make_instance()
    applied: list[dict] = []

    @contextmanager
    def session_scope():
        session = sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def apply(session, envelope):
        applied.append(envelope)

    app = FastAPI()
    app.include_router(build_replication_router(session_scope, apply))
    return app, applied


def _request(app, method: str, path: str, *, peer=TAILNET_PEER, **kwargs):
    """One request against the app as `peer` — run through the ASGI transport
    (async-only) under a private event loop, matching the suite's anyio style."""
    result = {}

    async def main():
        transport = httpx.ASGITransport(app=app, client=(peer, 4242))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://plugin"
        ) as client:
            result["response"] = await client.request(method, path, **kwargs)

    anyio.run(main)
    return result["response"]


def test_every_route_is_tailnet_gated(app_and_applied):
    """§5/§5.1: a peer outside SNOWLINE_TRUSTED_CIDRS (a hotel-LAN address) is
    refused on ingest AND admin routes; tailnet + loopback peers pass."""
    app, _ = app_and_applied
    for method, path in (
        ("POST", "/events/ingest"),
        ("GET", "/replication-admin/inbound"),
        ("POST", "/replication-admin/inbound"),
        ("GET", "/replication-admin/outbound"),
        ("GET", "/replication-admin/parked"),
    ):
        resp = _request(app, method, path, peer=HOTEL_LAN_PEER, json={})
        assert resp.status_code == 403, (method, path)
    for peer in (TAILNET_PEER, "127.0.0.1"):
        assert _request(app, "GET", "/replication-admin/inbound", peer=peer).status_code == 200


def test_trusted_cidrs_env_replaces_the_default(app_and_applied, monkeypatch):
    """The §5.1 config trap, pinned: setting SNOWLINE_TRUSTED_CIDRS REPLACES the
    default set — dropping the loopback entries is the outage."""
    app, _ = app_and_applied
    monkeypatch.setenv("SNOWLINE_TRUSTED_CIDRS", "100.64.0.0/10")
    assert _request(app, "GET", "/replication-admin/inbound", peer="127.0.0.1").status_code == 403
    assert _request(app, "GET", "/replication-admin/inbound", peer=TAILNET_PEER).status_code == 200


def test_handshake_ingest_and_parked_view_over_http(app_and_applied):
    """The §5 receiver side over HTTP: register (secret minted + returned ONCE,
    never listed), then a signed delivery through the ingest route lands in the
    apply seam, and the parked view answers (empty — the standing invariant)."""
    app, applied = app_and_applied

    reg = _request(
        app, "POST", "/replication-admin/inbound",
        json={"source_id": "roam.plugin", "epoch": "e1"},
    )
    assert reg.status_code == 200
    secret = reg.json()["secret"]

    listed = _request(app, "GET", "/replication-admin/inbound").json()
    assert [l["source_id"] for l in listed] == ["roam.plugin"]
    assert "secret" not in listed[0]

    # A duplicate registration refuses loudly (re-pairing mints a fresh epoch).
    dup = _request(
        app, "POST", "/replication-admin/inbound",
        json={"source_id": "roam.plugin", "epoch": "e1"},
    )
    assert dup.status_code == 409

    envelope = build_envelope(
        "thing.recorded", {"id": "x"},
        source_id="roam.plugin", epoch="e1", seq=1, peer_seen=0,
    )
    body = json.dumps(envelope).encode()
    resp = _request(
        app, "POST", "/events/ingest",
        content=body,
        headers={"X-Snowline-Signature": f"sha256={sign_body(secret, body)}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    assert [e["payload"]["id"] for e in applied] == ["x"]

    # A tampered body is a 401 rejection through the same route.
    resp = _request(
        app, "POST", "/events/ingest",
        content=body + b" ",
        headers={"X-Snowline-Signature": f"sha256={sign_body(secret, body)}"},
    )
    assert resp.status_code == 401

    assert _request(app, "GET", "/replication-admin/parked").json() == []


def test_rotate_and_retire_inbound_over_http(app_and_applied):
    app, _ = app_and_applied
    _request(
        app, "POST", "/replication-admin/inbound",
        json={"source_id": "roam.plugin", "epoch": "e1"},
    )
    rotated = _request(
        app, "POST", "/replication-admin/inbound/rotate",
        json={"source_id": "roam.plugin", "epoch": "e1"},
    )
    assert rotated.status_code == 200 and rotated.json()["secret"]
    assert rotated.json()["rotation_pending"] is True

    retired = _request(
        app, "POST", "/replication-admin/inbound/retire",
        json={"source_id": "roam.plugin", "epoch": "e1"},
    )
    assert retired.status_code == 200 and retired.json()["active"] is False
    missing = _request(
        app, "POST", "/replication-admin/inbound/rotate",
        json={"source_id": "roam.plugin", "epoch": "e1"},
    )
    assert missing.status_code == 404  # retired streams don't rotate


def test_outbound_management_over_http(app_and_applied):
    """The sender side of pairing over HTTP: create carries the handshake's
    secret + epoch, listing never echoes the secret, retire + the sender-side
    rotation swap both answer."""
    app, _ = app_and_applied
    created = _request(
        app, "POST", "/replication-admin/outbound",
        json={
            "target_url": "http://primary.plugin/events/ingest",
            "secret": "handshake-minted",
            "event_types": ["thing.recorded"],
            "epoch": "e1",
            "peer_source_id": "primary.plugin",
        },
    )
    assert created.status_code == 200
    sub = created.json()
    assert sub["source_id"] == "test.plugin"  # stamped from env at creation
    assert "secret" not in sub

    listed = _request(app, "GET", "/replication-admin/outbound").json()
    assert [s["id"] for s in listed] == [sub["id"]]

    swapped = _request(
        app, "POST", "/replication-admin/outbound/secret",
        json={"id": sub["id"], "secret": "rotated"},
    )
    assert swapped.status_code == 200

    retired = _request(
        app, "POST", "/replication-admin/outbound/retire", json={"id": sub["id"]}
    )
    assert retired.json()["active"] is False

    missing_field = _request(app, "POST", "/replication-admin/outbound", json={})
    assert missing_field.status_code == 400


def test_outbound_body_validation_edges(app_and_applied):
    """Presence-not-truthiness on required fields (an empty event_types list is
    a legal value, not 'missing') and a type check where a bare string would
    list()-explode into characters."""
    app, _ = app_and_applied
    base = {
        "target_url": "http://p/events/ingest",
        "secret": "s",
        "epoch": "e-edge",
    }
    as_string = _request(
        app, "POST", "/replication-admin/outbound",
        json={**base, "event_types": "thing.recorded"},
    )
    assert as_string.status_code == 400
    assert "must be a list" in as_string.json()["detail"]

    empty_list = _request(
        app, "POST", "/replication-admin/outbound",
        json={**base, "event_types": []},
    )
    assert empty_list.status_code == 200
    assert empty_list.json()["event_types"] == []


def test_rejected_view_and_requeue_over_http(make_instance):
    """The sender-side dead-letter mirror (§3.1): /rejected surfaces a
    vocabulary-rejected row next to /parked, and the requeue action puts it
    back on its stream."""
    import httpx as _httpx

    from snowline_plugin_sdk.replication import emit

    sessions = make_instance()

    @contextmanager
    def session_scope():
        session = sessions()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    app = FastAPI()
    app.include_router(build_replication_router(session_scope, lambda s, e: None))

    assert _request(app, "GET", "/replication-admin/rejected").json() == []

    # Dead-letter one row the real way: emit, then deliver into a vocabulary
    # rejection.
    with session_scope() as s:
        emit.create_outbound_subscription(
            s, "http://peer/events/ingest", "sec", ["thing.recorded"], epoch="e1"
        )
        emit.emit_event(s, "thing.recorded", {"n": 1})

    def reject(request: _httpx.Request) -> _httpx.Response:
        if request.method == "GET":
            return _httpx.Response(405)
        return _httpx.Response(
            401, json={"status": "rejected", "reason": "bad_signature"}
        )

    with session_scope() as s, _httpx.Client(
        transport=_httpx.MockTransport(reject)
    ) as client:
        emit.deliver_pending(s, client, reachability={})

    listed = _request(app, "GET", "/replication-admin/rejected").json()
    assert [(r["seq"], r["event_type"]) for r in listed] == [(1, "thing.recorded")]

    requeued = _request(
        app, "POST", "/replication-admin/rejected/requeue", json={"id": listed[0]["id"]}
    )
    assert requeued.status_code == 200 and requeued.json()["status"] == "pending"
    assert _request(app, "GET", "/replication-admin/rejected").json() == []

    missing = _request(
        app, "POST", "/replication-admin/rejected/requeue",
        json={"id": "00000000-0000-0000-0000-000000000000"},
    )
    assert missing.status_code == 404


def _reject_transport(reason: str, status: int):
    import httpx as _httpx

    def respond(request: _httpx.Request) -> _httpx.Response:
        if request.method == "GET":
            return _httpx.Response(405)
        return _httpx.Response(status, json={"status": "rejected", "reason": reason})

    return respond


def test_requeue_rejected_bulk_over_http(make_instance):
    """Issue #107 over HTTP: a whole cascade of rejected rows on one stream
    requeues in one call, returning the count."""
    import httpx as _httpx

    from snowline_plugin_sdk.replication import emit

    sessions = make_instance()

    @contextmanager
    def session_scope():
        session = sessions()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    app = FastAPI()
    app.include_router(build_replication_router(session_scope, lambda s, e: None))

    with session_scope() as s:
        sub = emit.create_outbound_subscription(
            s, "http://peer/events/ingest", "sec", ["thing.recorded"], epoch="e1"
        )
        emit.emit_event(s, "thing.recorded", {"n": 1})
        emit.emit_event(s, "thing.recorded", {"n": 2})

    with session_scope() as s, _httpx.Client(
        transport=_httpx.MockTransport(_reject_transport("malformed_envelope", 400))
    ) as client:
        emit.deliver_pending(s, client, reachability={})
        emit.deliver_pending(s, client, reachability={})

    assert len(_request(app, "GET", "/replication-admin/rejected").json()) == 2

    bulk = _request(
        app, "POST", "/replication-admin/rejected/requeue-bulk",
        json={"subscription_id": sub["id"]},
    )
    assert bulk.status_code == 200
    assert bulk.json() == {
        "subscription_id": sub["id"],
        "source_id": "test.plugin",
        "epoch": "e1",
        "requeued": 2,
    }
    assert _request(app, "GET", "/replication-admin/rejected").json() == []

    missing = _request(
        app, "POST", "/replication-admin/rejected/requeue-bulk",
        json={"subscription_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert missing.status_code == 404

    # A typo'd reason answers 400 naming the closed vocabulary — never a
    # silent `{"requeued": 0}` that reads as "already handled".
    typo = _request(
        app, "POST", "/replication-admin/rejected/requeue-bulk",
        json={"subscription_id": sub["id"], "reason": "bad_signatur"},
    )
    assert typo.status_code == 400
    assert "unknown rejection reason" in typo.json()["detail"]
    assert "bad_signature" in typo.json()["detail"]


def test_requeue_refuses_retired_subscription_over_http(make_instance):
    """Issue #108 over HTTP: both the per-row and the bulk requeue routes
    refuse a retired subscription with a 409 naming it (and its successor,
    when a re-pair created one) instead of resuming a stream nothing serves."""
    import httpx as _httpx

    from snowline_plugin_sdk.replication import emit

    sessions = make_instance()

    @contextmanager
    def session_scope():
        session = sessions()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    app = FastAPI()
    app.include_router(build_replication_router(session_scope, lambda s, e: None))

    with session_scope() as s:
        sub = emit.create_outbound_subscription(
            s, "http://peer/events/ingest", "sec", ["thing.recorded"], epoch="e1"
        )
        emit.emit_event(s, "thing.recorded", {"n": 1})

    with session_scope() as s, _httpx.Client(
        transport=_httpx.MockTransport(_reject_transport("bad_signature", 401))
    ) as client:
        emit.deliver_pending(s, client, reachability={})

    row_id = _request(app, "GET", "/replication-admin/rejected").json()[0]["id"]

    retire = _request(
        app, "POST", "/replication-admin/outbound/retire", json={"id": sub["id"]}
    )
    assert retire.json()["active"] is False

    refused = _request(
        app, "POST", "/replication-admin/rejected/requeue", json={"id": row_id}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "subscription_retired"
    assert "successor_subscription_id" not in refused.json()["detail"]

    refused_bulk = _request(
        app, "POST", "/replication-admin/rejected/requeue-bulk",
        json={"subscription_id": sub["id"]},
    )
    assert refused_bulk.status_code == 409
    assert refused_bulk.json()["detail"]["reason"] == "subscription_retired"

    # Untouched by either refused call.
    assert len(_request(app, "GET", "/replication-admin/rejected").json()) == 1

    # A re-pair mints a successor for the same peer — now named in the refusal.
    successor = _request(
        app, "POST", "/replication-admin/outbound",
        json={
            "target_url": "http://peer/events/ingest",
            "secret": "sec2",
            "event_types": ["thing.recorded"],
            "epoch": "e2",
        },
    ).json()
    refused_again = _request(
        app, "POST", "/replication-admin/rejected/requeue", json={"id": row_id}
    )
    assert refused_again.status_code == 409
    assert refused_again.json()["detail"]["successor_subscription_id"] == successor["id"]
