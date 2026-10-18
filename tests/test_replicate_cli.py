"""The `snowline replicate` argparse shell (issue #82): dispatch + exit codes,
driving the pairing/seed libraries against in-process instances via a patched
client. The library behaviors themselves are covered by
test_replication_pairing / test_replication_seed."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from snowline_platform import cli
from snowline_plugin_sdk.replication.models import (
    ReplicationInboundStream,
    ReplicationSubscription,
)

from ._replication_helpers import RoutedClient, make_participant, make_platform, plugin_entry


@pytest.fixture()
def two_instances(monkeypatch):
    prim_plugins = [
        plugin_entry("governance", "http://prim-gov", events=["decision.recorded"]),
    ]
    roam_plugins = [
        plugin_entry("governance", "http://roam-gov", events=["decision.recorded"]),
    ]
    apps = {
        "prim-platform": make_platform(plugins=prim_plugins),
        "prim-gov": make_participant(),
        "roam-platform": make_platform(plugins=roam_plugins),
        "roam-gov": make_participant(),
    }
    # The registry serves these same dicts, so a test can mutate them to
    # simulate an upgrade (refresh-vocabulary).
    apps["prim-platform"].plugins = prim_plugins
    apps["roam-platform"].plugins = roam_plugins
    client = RoutedClient({h: inst.app for h, inst in apps.items()})
    monkeypatch.setattr(cli, "_client", lambda: client)
    return apps


def test_pair_command_pairs_both_directions(two_instances, capsys):
    rc = cli.main([
        "replicate", "pair", "http://prim-platform",
        "--local-url", "http://roam-platform",
        "--local-instance", "roam", "--peer-instance", "primary",
    ])
    assert rc == 0
    # governance + platform paired, both directions → the spoke gov holds the
    # forward inbound registration.
    with two_instances["roam-gov"].scope() as s:
        assert [i.source_id for i in s.scalars(select(ReplicationInboundStream)).all()] \
            == ["primary.governance"]
    assert "paired 2 participant(s)" in capsys.readouterr().out


def test_pair_local_peer_url_points_reverse_streams_at_this_gateway(two_instances, capsys, monkeypatch):
    """Two-box pairing: the reverse (primary->roam) outbound subscriptions the
    hub creates must dial ROAM'S GATEWAY as the hub reaches it
    (`--local-peer-url`), through `/via/<plugin>`, not roam's loopback."""
    apps = two_instances
    # The same roam platform app, reachable by the peer under its tailnet name.
    client = RoutedClient({
        **{h: inst.app for h, inst in apps.items()},
        "roam-public": apps["roam-platform"].app,
    })
    monkeypatch.setattr(cli, "_client", lambda: client)
    rc = cli.main([
        "replicate", "pair", "http://prim-platform",
        "--local-url", "http://roam-platform",
        "--local-peer-url", "http://roam-public",
        "--local-instance", "roam", "--peer-instance", "primary",
    ])
    assert rc == 0
    assert "WARN --local-peer-url" not in capsys.readouterr().err
    with apps["prim-gov"].scope() as s:
        targets = [o.target_url for o in s.scalars(select(ReplicationSubscription)).all()]
    assert targets == ["http://roam-public/via/governance/events/ingest"]
    with apps["prim-platform"].scope() as s:
        targets = [o.target_url for o in s.scalars(select(ReplicationSubscription)).all()]
    assert targets == ["http://roam-public/replication/events/ingest"]


def test_pair_without_local_peer_url_warns_about_loopback_targets(two_instances, capsys):
    rc = cli.main([
        "replicate", "pair", "http://prim-platform",
        "--local-url", "http://roam-platform",
        "--local-instance", "roam", "--peer-instance", "primary",
    ])
    assert rc == 0
    assert "WARN --local-peer-url not given" in capsys.readouterr().err


def test_pair_dry_run_touches_nothing(two_instances, capsys):
    rc = cli.main([
        "replicate", "pair", "http://prim-platform",
        "--local-url", "http://roam-platform",
        "--local-instance", "roam", "--peer-instance", "primary", "--dry-run",
    ])
    assert rc == 0
    with two_instances["roam-gov"].scope() as s:
        assert s.scalars(select(ReplicationInboundStream)).all() == []
    assert "dry-run" in capsys.readouterr().out


def test_pair_missing_local_instance_errors(monkeypatch, capsys):
    monkeypatch.delenv("SNOWLINE_INSTANCE_ID", raising=False)
    rc = cli.main([
        "replicate", "pair", "http://prim-platform",
        "--local-url", "http://roam-platform", "--peer-instance", "primary",
    ])
    assert rc == 1
    assert "local-instance" in capsys.readouterr().err


def test_contract_version_mismatch_exit_code(monkeypatch, capsys):
    apps = {
        "prim-platform": make_platform(plugins=[
            plugin_entry("governance", "http://prim-gov", contract_version=2,
                         events=["decision.recorded"]),
        ]),
        "prim-gov": make_participant(),
        "roam-platform": make_platform(plugins=[
            plugin_entry("governance", "http://roam-gov", contract_version=3,
                         events=["decision.recorded"]),
        ]),
        "roam-gov": make_participant(),
    }
    client = RoutedClient({h: inst.app for h, inst in apps.items()})
    monkeypatch.setattr(cli, "_client", lambda: client)
    rc = cli.main([
        "replicate", "pair", "http://prim-platform",
        "--local-url", "http://roam-platform",
        "--local-instance", "roam", "--peer-instance", "primary",
    ])
    assert rc == 1
    assert "contract_version mismatch" in capsys.readouterr().err


# --- `replicate union` (§7.1, #98) -------------------------------------------


def test_union_dry_run_reports_live_pairing_state(two_instances, capsys):
    rc = cli.main([
        "replicate", "union", "http://prim-platform",
        "--local-url", "http://roam-platform",
        "--local-instance", "roam", "--peer-instance", "primary", "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "'governance' live pairing is unpaired" in out
    with two_instances["roam-gov"].scope() as s:
        assert s.scalars(select(ReplicationInboundStream)).all() == []


def test_union_pairs_first_and_warns_on_participants_without_enumerators(two_instances, capsys):
    """The recording participants declare no backfill enumerator: the union
    still pairs the live streams (step 1), then WARNS per side that their
    history is not unioned — loud, never silent — and leaves nothing open."""
    rc = cli.main([
        "replicate", "union", "http://prim-platform",
        "--local-url", "http://roam-platform",
        "--local-instance", "roam", "--peer-instance", "primary", "--poll", "0",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "paired 2 participant(s)" in out
    assert "has no backfill enumerator" in out
    assert "triage the §6.1 unreconciled view ON BOTH INSTANCES" in out
    with two_instances["prim-gov"].scope() as s:
        active = [i.source_id for i in s.scalars(select(ReplicationInboundStream)) if i.active]
    assert active == ["roam.governance"]  # the live stream only — no dangling backfill


def test_union_timeout_is_a_clean_error_exit(monkeypatch, capsys):
    def one_row(session):
        yield ("decision.recorded", {"id": "d1"})

    apps = {
        "prim-platform": make_platform(plugins=[
            plugin_entry("governance", "http://prim-gov", events=["decision.recorded"]),
        ]),
        "prim-gov": make_participant(enumerator=one_row),
        "roam-platform": make_platform(plugins=[
            plugin_entry("governance", "http://roam-gov", events=["decision.recorded"]),
        ]),
        "roam-gov": make_participant(enumerator=one_row),
    }
    client = RoutedClient({h: inst.app for h, inst in apps.items()})
    monkeypatch.setattr(cli, "_client", lambda: client)
    rc = cli.main([
        "replicate", "union", "http://prim-platform",
        "--local-url", "http://roam-platform",
        "--local-instance", "roam", "--peer-instance", "primary",
        "--poll", "0", "--timeout", "0",
    ])
    assert rc == 1
    assert "timed out waiting for backfill completion" in capsys.readouterr().err


def test_refresh_vocabulary_command(two_instances, capsys):
    """`replicate refresh-vocabulary`: after both sides gain an event type, the
    verb widens the paired streams (dry-run first changes nothing), exits 0,
    and a re-run reports already current."""
    args = [
        "http://prim-platform",
        "--local-url", "http://roam-platform",
        "--local-instance", "roam", "--peer-instance", "primary",
    ]
    assert cli.main(["replicate", "pair", *args]) == 0
    for host in ("prim-platform", "roam-platform"):
        (entry,) = two_instances[host].plugins
        entry["manifest"]["replication"]["events"].append("decision.amended")
    capsys.readouterr()

    assert cli.main(["replicate", "refresh-vocabulary", *args, "--dry-run"]) == 0
    assert "would add ['decision.amended']" in capsys.readouterr().out
    with two_instances["roam-gov"].scope() as s:
        assert s.scalars(select(ReplicationSubscription)).one().event_types == [
            "decision.recorded"
        ]

    assert cli.main(["replicate", "refresh-vocabulary", *args]) == 0
    assert "added ['decision.amended']" in capsys.readouterr().out
    with two_instances["roam-gov"].scope() as s:
        assert s.scalars(select(ReplicationSubscription)).one().event_types == [
            "decision.recorded", "decision.amended",
        ]

    assert cli.main(["replicate", "refresh-vocabulary", *args]) == 0
    assert "already current" in capsys.readouterr().out


# -- #228: `seed --reverse-pair` is idempotent and takes --participants ------


def _reverse_seed_config(tmp_path):
    import json

    path = tmp_path / "seed.json"
    path.write_text(json.dumps({
        "primary": {"platform_url": "http://prim-platform", "instance": "primary"},
        "spoke": {"platform_url": "http://roam-platform", "instance": "roam"},
        "participants": {
            "governance": {
                "spoke_ingest_url": "http://roam-gov/events/ingest",
                "spoke_db_url": f"sqlite:///{tmp_path}/gov.db",
            },
        },
    }))
    return str(path)


def test_seed_reverse_pair_rerun_is_idempotent(two_instances, tmp_path, capsys):
    cfg = _reverse_seed_config(tmp_path)
    argv = ["replicate", "seed", "--config", cfg, "--reverse-pair"]
    assert cli.main(argv) == 0
    capsys.readouterr()
    assert cli.main(argv) == 0  # previously: PairingError from the run-once guard
    assert "governance: already paired" in capsys.readouterr().out
    with two_instances["prim-gov"].scope() as s:
        active = [i for i in s.scalars(select(ReplicationInboundStream)).all()
                  if i.source_id == "roam.governance" and i.active]
    assert len(active) == 1


def test_seed_reverse_pair_participants_unknown_name_refused(two_instances, tmp_path, capsys):
    cfg = _reverse_seed_config(tmp_path)
    rc = cli.main(["replicate", "seed", "--config", cfg, "--reverse-pair",
                   "--participants", "governance,nope"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown participant(s) ['nope']" in err and "['governance']" in err


def test_seed_participants_requires_reverse_pair(two_instances, tmp_path, capsys):
    cfg = _reverse_seed_config(tmp_path)
    rc = cli.main(["replicate", "seed", "--config", cfg, "--participants", "governance"])
    assert rc == 1
    assert "only with --reverse-pair" in capsys.readouterr().err


def test_seed_participants_list_tolerates_spaces(two_instances, tmp_path, monkeypatch):
    from snowline_platform import replication_seed as seed_mod

    seen = {}

    def fake_reverse_pair(client, cfg, *, participants=None, force=False, report=print):
        seen["participants"], seen["force"] = participants, force
        return []

    monkeypatch.setattr(seed_mod, "run_reverse_pair", fake_reverse_pair)
    cfg = _reverse_seed_config(tmp_path)
    rc = cli.main(["replicate", "seed", "--config", cfg, "--reverse-pair",
                   "--participants", "governance, pm", "--force"])
    assert rc == 0
    assert seen == {"participants": ["governance", "pm"], "force": True}


def test_seed_force_requires_reverse_pair(two_instances, tmp_path, capsys):
    cfg = _reverse_seed_config(tmp_path)
    assert cli.main(["replicate", "seed", "--config", cfg, "--force"]) == 1
    assert "--force applies only with --reverse-pair" in capsys.readouterr().err
