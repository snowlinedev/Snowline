"""Skew guard for the platform's own replication stream (decision 54447516,
N-1 train compatibility; #216): scope.* and milestone.*.

  A. every payload the full write surface emits carries exactly the declared
     `PAYLOAD_KEYS | WIDENED_KEYS` for its event type;
  B. every widened key has an apply-side presence guard in `scopes.py` /
     `milestones.py` (ast tripwire).

Mechanism: `snowline_plugin_sdk.replication.skew`; declarations beside the
vocabulary in `snowline_platform.replication`.
"""

from __future__ import annotations

import inspect

from snowline_plugin_sdk.replication import emit
from snowline_plugin_sdk.replication.models import ReplicationOutboxRow
from snowline_plugin_sdk.replication.skew import (
    check_emitted_payloads,
    find_unguarded_widened_keys,
)
from sqlalchemy import select

from snowline_platform import milestones, replication, scopes
from snowline_platform.replication import KEY_INVENTORY


def test_inventory_covers_exactly_the_platform_stream_vocabulary():
    assert KEY_INVENTORY.event_types == set(replication.PLATFORM_EVENTS)


def _drive_full_write_surface(db_session) -> dict[str, list[dict]]:
    emit.create_outbound_subscription(
        db_session,
        "http://peer/replication/events/ingest",
        "topsecret",
        sorted(KEY_INVENTORY.event_types),
        epoch="e1",
        source_id="hub.platform",
    )
    scopes.create(db_session, slug="acme", name="Acme", kind="org")
    scopes.create(db_session, slug="acme/repo", name="Repo", kind="project", parent="acme")
    scopes.update(db_session, "acme/repo", isolated=True)
    db_session.commit()
    milestones.create(db_session, "acme/repo", "alpha", outcome="ship it")
    milestones.create(db_session, "acme/repo", "beta")
    db_session.commit()
    milestones.update(db_session, "acme/repo/alpha", outcome="ship it well")
    milestones.activate(db_session, "acme/repo/alpha", reason="go")
    milestones.add_dependency(db_session, "acme/repo/beta", "acme/repo/alpha")
    db_session.commit()
    milestones.merge(db_session, "acme/repo/beta", "acme/repo/alpha")
    db_session.commit()
    out: dict[str, list[dict]] = {}
    for row in db_session.scalars(
        select(ReplicationOutboxRow).order_by(ReplicationOutboxRow.seq)
    ):
        out.setdefault(row.payload["event_type"], []).append(row.payload["payload"])
    return out


def test_full_write_surface_payload_keys_match_declared(db_session):
    """A: no undeclared key (silent widening), no stale declaration, and every
    declared event type is actually exercised."""
    driven = _drive_full_write_surface(db_session)
    errors = check_emitted_payloads(KEY_INVENTORY, driven)
    assert not errors, "\n".join(errors)


def test_backfill_enumerator_payload_keys_match_declared(db_session, monkeypatch):
    """A (backfill path): the §7.1 enumerator replays the live-driven state and
    adds the conditional per-register author stamps; they must be declared
    OPTIONAL (allowed, never required). Not every type is replayed by the
    enumerator, so coverage is not required - but the optional keys must
    actually appear, or this variant guards nothing."""
    # Authored rows store their source id only when replication is configured.
    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "hub.platform")
    _drive_full_write_surface(db_session)
    driven: dict[str, list[dict]] = {}
    for event_type, payload in replication.enumerate_backfill(db_session):
        driven.setdefault(event_type, []).append(payload)
    errors = check_emitted_payloads(KEY_INVENTORY, driven, require_all_types=False)
    assert not errors, "\n".join(errors)
    seen = {k for ps in driven.values() for p in ps for k in p}
    declared_optional = {k for ks in KEY_INVENTORY.optional_keys.values() for k in ks}
    assert declared_optional <= seen, (
        f"optional keys never emitted by the backfill drive: {sorted(declared_optional - seen)}"
    )


def test_every_widened_key_has_an_apply_side_presence_guard():
    """B: scope + milestone apply live in scopes.py / milestones.py."""
    source = inspect.getsource(scopes) + "\n" + inspect.getsource(milestones)
    errors = find_unguarded_widened_keys(KEY_INVENTORY, source)
    assert not errors, "\n".join(errors)
