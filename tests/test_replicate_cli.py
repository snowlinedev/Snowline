"""The `snowline replicate` argparse shell (issue #82): dispatch + exit codes,
driving the pairing/seed libraries against in-process instances via a patched
client. The library behaviors themselves are covered by
test_replication_pairing / test_replication_seed."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from snowline_platform import cli
from snowline_plugin_sdk.replication.models import ReplicationInboundStream

from ._replication_helpers import RoutedClient, make_participant, make_platform, plugin_entry


@pytest.fixture()
def two_instances(monkeypatch):
    apps = {
        "prim-platform": make_platform(plugins=[
            plugin_entry("governance", "http://prim-gov", events=["decision.recorded"]),
        ]),
        "prim-gov": make_participant(),
        "roam-platform": make_platform(plugins=[
            plugin_entry("governance", "http://roam-gov", events=["decision.recorded"]),
        ]),
        "roam-gov": make_participant(),
    }
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
