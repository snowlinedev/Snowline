"""`snowline replicate refresh-vocabulary` (pm#133): widening an already-paired
stream's event vocabulary after an upgrade, without a re-seed. Driven against
in-process instances over the REAL SDK admin surface (the same harness as
test_replication_pairing), so the new `POST <admin>/outbound/vocabulary` route
and the CLI library are exercised together.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from snowline_plugin_sdk.replication import emit as emit_mod
from snowline_plugin_sdk.replication.models import (
    ReplicationOutboxRow,
    ReplicationSubscription,
)
from sqlalchemy import select

from snowline_platform import replication_pairing as pairing

from ._replication_helpers import (
    RoutedClient,
    make_participant,
    make_platform,
    plugin_entry,
)

V1 = ["decision.recorded", "decision.superseded"]
NEW = "decision.amended"


def _instances(*, gov_versions=(2, 2)):
    """Primary + roam, governance on both. Returns the apps, the client, and the
    two governance `/plugins` entries (mutate their `events` to simulate an
    upgrade — the registry serves the same dicts)."""
    prim_gov_entry = plugin_entry(
        "governance",
        "http://prim-gov",
        contract_version=gov_versions[0],
        events=list(V1),
    )
    roam_gov_entry = plugin_entry(
        "governance",
        "http://roam-gov",
        contract_version=gov_versions[1],
        events=list(V1),
    )
    apps = {
        "prim-platform": make_platform(plugins=[prim_gov_entry]),
        "prim-gov": make_participant(),
        "roam-platform": make_platform(plugins=[roam_gov_entry]),
        "roam-gov": make_participant(),
    }
    client = RoutedClient({host: inst.app for host, inst in apps.items()})
    return apps, client, prim_gov_entry, roam_gov_entry


def _upgrade(entry, *new):
    entry["manifest"]["replication"]["events"] = [
        *entry["manifest"]["replication"]["events"],
        *new,
    ]


def _discover(client):
    local = pairing.discover_participants(client, "http://roam-platform", "roam")
    peer = pairing.discover_participants(client, "http://prim-platform", "primary")
    return local, peer


def _event_types(app) -> list[list[str]]:
    with app.scope() as s:
        return [
            list(sub.event_types) for sub in s.scalars(select(ReplicationSubscription))
        ]


def _quiet(_msg):
    pass


def _gov(plan):
    return [d for d in plan.directions if d.participant == "governance"]


def test_same_vocabulary_on_both_sides_is_a_no_op():
    apps, client, *_ = _instances()
    local, peer = _discover(client)
    pairing.pair(client, local, peer, report=_quiet)
    lines: list[str] = []
    plan = pairing.refresh_vocabulary(client, *_discover(client), report=lines.append)
    assert {d.status for d in plan.directions} == {"current"}
    assert len(plan.directions) == 4  # governance + platform, both directions
    assert any("already current" in line for line in lines)
    assert _event_types(apps["roam-gov"]) == [V1]


def test_upgrade_on_both_sides_widens_both_directions_and_new_type_flows(monkeypatch):
    """The pm#133 scenario: paired at V1, both sides then gain `decision.amended`.
    Before the refresh an emit of the new type queues nothing; after it the SAME
    stream (same epoch) carries it."""
    apps, client, prim_entry, roam_entry = _instances()
    pairing.pair(client, *_discover(client), report=_quiet)
    with apps["roam-gov"].scope() as s:
        (epoch_before,) = [
            sub.epoch for sub in s.scalars(select(ReplicationSubscription))
        ]

    _upgrade(prim_entry, NEW)
    _upgrade(roam_entry, NEW)
    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "roam.governance")
    with apps["roam-gov"].scope() as s:
        assert emit_mod.emit_event(s, NEW, {"id": "before"}) == []

    plan = pairing.refresh_vocabulary(client, *_discover(client), report=_quiet)
    gov = _gov(plan)
    assert [(d.sender, d.status, d.added) for d in gov] == [
        ("roam.governance", "widened", [NEW]),
        ("primary.governance", "widened", [NEW]),
    ]
    assert _event_types(apps["roam-gov"]) == [[*V1, NEW]]
    assert _event_types(apps["prim-gov"]) == [[*V1, NEW]]

    with apps["roam-gov"].scope() as s:
        (env,) = emit_mod.emit_event(s, NEW, {"id": "after"})
        assert s.scalars(select(ReplicationOutboxRow)).one().event_type == NEW
    assert env["epoch"] == epoch_before  # no re-seed, no new stream

    # Idempotent: a re-run reports every direction current and writes nothing.
    again = pairing.refresh_vocabulary(client, *_discover(client), report=_quiet)
    assert {d.status for d in again.directions} == {"current"}


def test_receiver_lacking_the_type_holds_it_back():
    """Only the SENDER upgraded: its stream is widened to sender ∩ receiver, so
    the new type is held back (the receiver would only park it, decision
    54447516); once the receiver upgrades, a re-run widens it."""
    apps, client, prim_entry, roam_entry = _instances()
    pairing.pair(client, *_discover(client), report=_quiet)
    _upgrade(roam_entry, NEW)

    plan = pairing.refresh_vocabulary(client, *_discover(client), report=_quiet)
    roam_to_prim, prim_to_roam = _gov(plan)
    assert roam_to_prim.status == "current" and roam_to_prim.held_back == [NEW]
    assert roam_to_prim.target == V1
    assert prim_to_roam.status == "current" and prim_to_roam.held_back == []
    assert _event_types(apps["roam-gov"]) == [V1]

    _upgrade(prim_entry, NEW)
    plan = pairing.refresh_vocabulary(client, *_discover(client), report=_quiet)
    assert [d.added for d in _gov(plan)] == [[NEW], [NEW]]


def test_dry_run_reports_without_widening():
    apps, client, prim_entry, roam_entry = _instances()
    pairing.pair(client, *_discover(client), report=_quiet)
    _upgrade(prim_entry, NEW)
    _upgrade(roam_entry, NEW)
    plan = pairing.refresh_vocabulary(
        client, *_discover(client), dry_run=True, report=_quiet
    )
    assert [(d.status, d.added) for d in _gov(plan)] == [
        ("would-widen", [NEW]),
        ("would-widen", [NEW]),
    ]
    assert _event_types(apps["roam-gov"]) == [V1]
    assert _event_types(apps["prim-gov"]) == [V1]


def test_never_narrows_when_a_side_drops_a_type():
    """A side that stops declaring a type does not shrink the live stream."""
    apps, client, prim_entry, _ = _instances()
    pairing.pair(client, *_discover(client), report=_quiet)
    prim_entry["manifest"]["replication"]["events"] = ["decision.recorded"]
    pairing.refresh_vocabulary(client, *_discover(client), report=_quiet)
    assert _event_types(apps["roam-gov"]) == [V1]
    assert _event_types(apps["prim-gov"]) == [V1]


def test_contract_version_mismatch_refuses_the_direction_others_proceed():
    apps, client, prim_entry, roam_entry = _instances()
    pairing.pair(client, *_discover(client), report=_quiet)
    prim_entry["manifest"]["replication"]["contract_version"] = 3
    _upgrade(prim_entry, NEW)
    _upgrade(roam_entry, NEW)
    plan = pairing.refresh_vocabulary(client, *_discover(client), report=_quiet)
    assert {d.status for d in _gov(plan)} == {"refused"}
    assert len(plan.refused) == 2
    assert {d.status for d in plan.directions if d.participant == "platform"} == {
        "current"
    }
    assert _event_types(apps["roam-gov"]) == [V1]  # nothing widened


def test_unpaired_direction_is_reported_not_created():
    apps, client, *_ = _instances()
    plan = pairing.refresh_vocabulary(client, *_discover(client), report=_quiet)
    assert {d.status for d in plan.directions} == {"unpaired"}
    assert _event_types(apps["roam-gov"]) == []


def test_one_sided_participant_is_skipped():
    _, client, *_ = _instances()
    local, peer = _discover(client)
    local["memory"] = pairing.Participant(
        name="memory",
        admin_base="http://roam-mem/replication-admin",
        ingest_url="http://roam-mem/events/ingest",
        source_id="roam.memory",
        events=("memory.set",),
        contract_version=2,
    )
    plan = pairing.refresh_vocabulary(client, local, peer, report=_quiet)
    assert plan.one_sided == ["memory"]
    assert all(d.participant != "memory" for d in plan.directions)


def test_sender_sdk_predating_the_route_raises_a_labeled_error():
    """A sender whose SDK has no `/outbound/vocabulary` route answers the
    framework 404 — surfaced as an upgrade-first PairingError, not a raw 404."""
    old = FastAPI()
    sub = {
        "id": "00000000-0000-0000-0000-000000000001",
        "active": True,
        "source_id": "roam.governance",
        "peer_source_id": "primary.governance",
        "event_types": list(V1),
        "target_url": "http://prim-gov/events/ingest",
    }

    @old.get("/replication-admin/outbound")
    async def list_outbound() -> list[dict]:
        return [sub]

    client = RoutedClient({"old-gov": old})
    sender = pairing.Participant(
        name="governance",
        admin_base="http://old-gov/replication-admin",
        ingest_url="http://old-gov/events/ingest",
        source_id="roam.governance",
        events=(*V1, NEW),
        contract_version=2,
    )
    receiver = pairing.Participant(
        name="governance",
        admin_base="http://prim-gov/replication-admin",
        ingest_url="http://prim-gov/events/ingest",
        source_id="primary.governance",
        events=(*V1, NEW),
        contract_version=2,
    )
    with pytest.raises(pairing.PairingError, match="predates refresh-vocabulary"):
        pairing.refresh_direction(client, sender, receiver)
