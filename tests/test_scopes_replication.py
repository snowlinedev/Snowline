"""Scopes adopting the replication contract (replication-continuity spec §8,
issue #81): `create`/`update` emitting into the transactional outbox, and
`scopes.apply_scope_event` as the domain apply function driven through the
SDK's `ingest_delivery` (issue #77) — the append-mostly happy path, the named
cross-partition slug collision, and the ordering/watermark self-heal.

Envelopes are hand-built (mirroring the SDK's own `test_replication_ingest.py`)
rather than run through a second live instance: one Postgres database plays
"this instance's store" for the apply side, which is exactly what
`apply_scope_event` reads/writes against in production.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from snowline_platform import scopes
from snowline_plugin_sdk.contract import EVENT_SCOPE_CREATED, EVENT_SCOPE_UPDATED
from snowline_plugin_sdk.replication import emit, ingest
from snowline_plugin_sdk.replication.envelope import build_envelope, sign_body
from snowline_plugin_sdk.replication.models import (
    ReplicationInboundStream,
    ReplicationOutboxRow,
)

STREAM = ("roam.platform", "epoch-1")


def _register(session):
    out = ingest.register_inbound_stream(session, *STREAM)
    session.commit()  # registration precedes deliveries in its own transaction
    return out


def _payload(slug, name, kind, *, parent=None, isolated=False, status="active", scope_id=None):
    return {
        "id": str(scope_id or uuid.uuid4()),
        "slug": slug,
        "name": name,
        "kind": kind,
        "parent": parent,
        "isolated": isolated,
        "status": status,
    }


def _delivery(secret, event_type, payload, seq):
    envelope = build_envelope(
        event_type, payload, source_id=STREAM[0], epoch=STREAM[1], seq=seq, peer_seen=0
    )
    body = json.dumps(envelope).encode()
    return body, f"sha256={sign_body(secret, body)}"


def _deliver(session, secret, event_type, payload, seq, **kw):
    """One delivery per transaction — the ingest TRANSACTION CONTRACT the
    admin route's per-request `session_scope` provides in production."""
    body, sig = _delivery(secret, event_type, payload, seq)
    out = ingest.ingest_delivery(session, body, sig, scopes.apply_scope_event, **kw)
    session.commit()
    return out


def _stream(session):
    return session.get(ReplicationInboundStream, STREAM)


# --- emit side: create/update write the transactional outbox -----------------


def test_create_and_update_emit_matching_outbox_rows(db_session):
    secret = "topsecret"
    emit.create_outbound_subscription(
        db_session,
        "http://peer/replication/events/ingest",
        secret,
        [EVENT_SCOPE_CREATED, EVENT_SCOPE_UPDATED],
        epoch="e1",
        source_id="hub.platform",
    )
    db_session.commit()

    org = scopes.create(db_session, slug="acme", name="Acme", kind="org")
    db_session.commit()
    created_payload = scopes.to_replication_payload(org)  # captured BEFORE update mutates org in place
    scopes.update(db_session, "acme", isolated=True)
    db_session.commit()

    rows = db_session.scalars(
        select(ReplicationOutboxRow).order_by(ReplicationOutboxRow.seq)
    ).all()
    assert [r.payload["event_type"] for r in rows] == [
        EVENT_SCOPE_CREATED,
        EVENT_SCOPE_UPDATED,
    ]
    assert rows[0].payload["payload"] == created_payload
    assert rows[1].payload["payload"]["isolated"] is True
    assert rows[1].payload["payload"]["id"] == str(org.id)


def test_create_without_a_subscription_emits_nothing(db_session):
    """No outbound stream (the pre-pairing default, §9 item 6) — emitting is a
    harmless no-op, not an error."""
    scopes.create(db_session, slug="acme", name="Acme", kind="org")
    db_session.commit()
    assert db_session.scalars(select(ReplicationOutboxRow)).all() == []


# --- apply side: the append-mostly happy path ---------------------------------


def test_apply_append_mostly_happy_path_parent_by_slug(db_session):
    """§8/§9 item 5's core case: a scope's create/update applies in order, and
    a child resolves its parent by the REPLICATED SLUG — never a foreign
    `parent_id` — landing on the correct LOCAL row."""
    secret = _register(db_session)["secret"]

    org_payload = _payload("acme", "Acme", "org")
    status, resp = _deliver(db_session, secret, EVENT_SCOPE_CREATED, org_payload, 1)
    assert (status, resp["status"]) == (200, "applied")
    org = scopes.resolve(db_session, "acme")
    assert org is not None and str(org.id) == org_payload["id"]

    child_payload = _payload("acme/repo", "Repo", "project", parent="acme")
    status, resp = _deliver(db_session, secret, EVENT_SCOPE_CREATED, child_payload, 2)
    assert (status, resp["status"]) == (200, "applied")
    child = scopes.resolve(db_session, "acme/repo")
    assert child.parent_id == org.id

    status, resp = _deliver(
        db_session, secret, EVENT_SCOPE_UPDATED, {**child_payload, "isolated": True}, 3
    )
    assert (status, resp["status"]) == (200, "applied")
    assert scopes.resolve(db_session, "acme/repo").isolated is True

    assert (_stream(db_session).gate_seq, _stream(db_session).applied_seq) == (3, 3)

    # Redelivery is a watermark no-op — apply (which would otherwise raise
    # ScopeConflictError against its own prior write) never re-runs.
    status, resp = _deliver(db_session, secret, EVENT_SCOPE_CREATED, org_payload, 1)
    assert (status, resp["status"]) == (200, "duplicate")


def test_apply_dangling_child_does_not_derive_a_local_parent(db_session):
    """Regression: a replicated scope's `parent_id` must replay the ORIGIN's
    OWN resolved value verbatim, never re-derived from whatever THIS instance
    happens to locally hold under the same slug prefix.

    Setup: the replica already has its own "acme" (a genuinely unrelated
    scope — different id, coincidentally the same slug). The origin authored
    "acme/repo" with NO parent at all (its own "acme" didn't exist when it was
    created there, so `payload["parent"]` is `None`). Applying that create
    here must leave `parent_id` at `None` too — matching the origin — instead
    of silently attaching the replica's unrelated local "acme". Silently
    diverging here would poison §6.1: governance's ancestor walk goes through
    `parent_id`, so the two instances would compute different applicability
    for the SAME scope after a heal."""
    local_acme = scopes.create(db_session, slug="acme", name="Local Acme", kind="org")
    db_session.commit()

    secret = _register(db_session)["secret"]
    dangling_payload = _payload("acme/repo", "Repo", "project", parent=None)
    status, resp = _deliver(
        db_session, secret, EVENT_SCOPE_CREATED, dangling_payload, 1
    )
    assert (status, resp["status"]) == (200, "applied")

    replica_repo = scopes.resolve(db_session, "acme/repo")
    assert replica_repo.parent_id is None
    assert replica_repo.parent_id != local_acme.id


# --- the named cross-partition slug collision (§8) ----------------------------


def test_slug_collision_parks_then_needs_manual_resolution(db_session):
    """spec §8, verbatim: 'slug collisions across a partition fail loud at
    ingest and require manual resolution.' Every apply exception is
    §8.1-retryable — a collision goes through the SAME bounded retry-then-park
    path as any other apply failure; parking's loud, re-appliable state IS the
    'fail loud, manual resolution' the spec calls for, not a second mechanism."""
    local = scopes.create(db_session, slug="acme", name="Acme HQ", kind="org")
    db_session.commit()

    secret = _register(db_session)["secret"]
    peer_payload = _payload("acme", "Acme (peer)", "org")
    assert peer_payload["id"] != str(local.id)

    for attempt in (1, 2):
        status, resp = _deliver(
            db_session, secret, EVENT_SCOPE_CREATED, peer_payload, 1, park_after=3
        )
        assert (status, resp["reason"]) == (503, "apply_failed")
        assert resp["attempts"] == attempt
        assert "already exists" in resp["error"]

    status, resp = _deliver(
        db_session, secret, EVENT_SCOPE_CREATED, peer_payload, 1, park_after=3
    )
    assert (status, resp["status"]) == (200, "parked")

    parked = ingest.list_parked(db_session)
    assert len(parked) == 1
    assert parked[0]["payload"]["payload"]["slug"] == "acme"
    assert "already exists" in parked[0]["reason"]

    # The local scope survives untouched — a failed apply rolls back cleanly.
    assert scopes.resolve(db_session, "acme").id == local.id

    # The gate advanced past the park — the stream flows; an unrelated scope
    # behind the collision still applies normally.
    other_payload = _payload("globex", "Globex", "org")
    status, resp = _deliver(db_session, secret, EVENT_SCOPE_CREATED, other_payload, 2)
    assert (status, resp["status"]) == (200, "applied")
    assert (_stream(db_session).gate_seq, _stream(db_session).applied_seq) == (2, 0)

    # Manual resolution (spec §8): an operator retires the LOSING local scope
    # (slugs are never renamed/reused by design — spec §2 — so there is no
    # rename-out-of-the-way path), freeing the slug, then re-applies from the
    # park.
    db_session.delete(local)
    db_session.commit()
    out = ingest.reapply_parked(db_session, *STREAM, 1, scopes.apply_scope_event)
    db_session.commit()

    assert ingest.list_parked(db_session) == []
    assert out["applied_seq"] == 2  # frontier unpinned through the re-applied seq
    peer_scope = scopes.resolve(db_session, "acme")
    assert str(peer_scope.id) == peer_payload["id"]


# --- ordering: an unknown scope slug is retryable, then parks if it never
# --- materializes (§8's ordering note / §8.1) ---------------------------------


def test_unknown_parent_slug_is_retryable_and_self_heals(db_session):
    secret = _register(db_session)["secret"]
    child_payload = _payload("acme/repo", "Repo", "project", parent="acme")

    status, resp = _deliver(
        db_session, secret, EVENT_SCOPE_CREATED, child_payload, 1, park_after=5
    )
    assert (status, resp["reason"]) == (503, "apply_failed")
    assert "acme" in resp["error"]
    assert scopes.resolve(db_session, "acme/repo") is None

    # The parent shows up locally by whatever means — ordinary scope-stream
    # lag, not a poison event.
    scopes.create(db_session, slug="acme", name="Acme", kind="org")
    db_session.commit()

    status, resp = _deliver(
        db_session, secret, EVENT_SCOPE_CREATED, child_payload, 1, park_after=5
    )
    assert (status, resp["status"]) == (200, "applied")
    assert (
        scopes.resolve(db_session, "acme/repo").parent_id
        == scopes.resolve(db_session, "acme").id
    )


def test_parent_that_never_arrives_parks_after_the_bound(db_session):
    """§8.1: an unknown slug that never materializes must not stall the stream
    forever — it parks like any other poison event."""
    secret = _register(db_session)["secret"]
    child_payload = _payload("acme/repo", "Repo", "project", parent="acme")

    for _ in (1, 2):
        status, resp = _deliver(
            db_session, secret, EVENT_SCOPE_CREATED, child_payload, 1, park_after=3
        )
        assert (status, resp["reason"]) == (503, "apply_failed")

    status, resp = _deliver(
        db_session, secret, EVENT_SCOPE_CREATED, child_payload, 1, park_after=3
    )
    assert (status, resp["status"]) == (200, "parked")
    assert scopes.resolve(db_session, "acme/repo") is None
    assert len(ingest.list_parked(db_session)) == 1
