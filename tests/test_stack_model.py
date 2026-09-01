"""The pure `snowline stack sync` logic (item b70b0359 / issue #203):
train-manifest parsing, the spoke-only guard, migration-crossed detection,
symlink-swap planning + GC, env-template drift, the auto-mode failure-posture
split, and the run report shape.

Everything here runs without a checkout, a subprocess, or a real HOME — that
is the point of keeping `stack/model.py` side-effect free. The orchestration
on top of it is covered by `test_stack_sync.py` against a fake runner.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snowline_platform.stack import model as m

TRAIN_JSON = {
    "version": "v0.1.0",
    "components": {
        "platform": {
            "repo": "snowlinedev/Snowline", "tag": "v0.1.0", "sha": "a" * 40,
            "wheel": "snowline_platform-0.1.0-py3-none-any.whl", "kind": "service",
        },
        "governance": {
            "repo": "snowlinedev/Snowline", "tag": "v0.1.0", "sha": "a" * 40,
            "wheel": "snowline_governance-0.1.0-py3-none-any.whl", "kind": "service",
        },
        "memory": {
            "repo": "snowlinedev/Snowline", "tag": "v0.1.0", "sha": "a" * 40,
            "wheel": "snowline_memory-0.1.0-py3-none-any.whl", "kind": "service",
        },
        "sdk": {
            "repo": "snowlinedev/Snowline", "tag": "v0.1.0", "sha": "a" * 40,
            "wheel": "snowline_plugin_sdk-0.1.0-py3-none-any.whl", "kind": "library",
        },
        "pm": {
            "repo": "snowlinedev/snowline-pm", "tag": "v0.1.0", "sha": "b" * 40,
            "wheel": "snowline_pm-0.1.0-py3-none-any.whl", "kind": "service",
        },
    },
}


# -- train manifest ---------------------------------------------------------


def test_parse_train_manifest_matches_the_real_v0_1_0_shape():
    manifest = m.parse_train_manifest(TRAIN_JSON)
    assert manifest.version == "v0.1.0"
    assert {s.service for s in manifest.installable_services()} == {
        "platform", "governance", "memory", "pm",
    }
    assert manifest.sdk().service == "sdk"
    assert manifest.sdk().kind == "library"
    assert manifest.repos() == ("snowlinedev/Snowline", "snowlinedev/snowline-pm")


def test_manifest_service_lookup_raises_for_unknown_service():
    manifest = m.parse_train_manifest(TRAIN_JSON)
    with pytest.raises(m.StackError, match="no entry for service"):
        manifest.service("walkthrough")


def test_manifest_requires_a_library_kind_entry_to_resolve_sdk():
    data = json.loads(json.dumps(TRAIN_JSON))
    del data["components"]["sdk"]
    manifest = m.parse_train_manifest(data)
    with pytest.raises(m.StackError, match="library-kind"):
        manifest.sdk()


def test_validate_train_version_rejects_non_v0_scheme():
    with pytest.raises(m.StackError, match="v0.MINOR.PATCH"):
        m.validate_train_version("1.0.0")
    with pytest.raises(m.StackError, match="v0.MINOR.PATCH"):
        m.validate_train_version("v1.0.0")


def test_resolve_train_version_prefers_explicit_over_latest():
    assert m.resolve_train_version("v0.2.0", "v0.3.0") == "v0.2.0"
    assert m.resolve_train_version(None, "v0.3.0") == "v0.3.0"


# -- stack.json ---------------------------------------------------------


def test_stack_config_round_trips_through_json():
    cfg = m.StackConfig(role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net")
    parsed = m.parse_stack_config(json.loads(cfg.to_json()))
    assert parsed == cfg


def test_stack_config_requires_every_field():
    with pytest.raises(m.StackError, match="instance_id"):
        m.parse_stack_config({"role": "spoke", "primary_tailnet_address": "x"})


def test_load_stack_config_is_none_when_absent():
    assert m.load_stack_config(None) is None


# -- spoke-only posture (decision 54447516) --------------------------------


def test_check_spoke_posture_accepts_a_fresh_spoke_config():
    m.check_spoke_posture(requested_role="spoke", existing=None)
    m.check_spoke_posture(
        requested_role="spoke",
        existing=m.StackConfig(role="spoke", instance_id="roam", primary_tailnet_address="x"),
    )


def test_check_spoke_posture_refuses_a_non_spoke_role():
    with pytest.raises(m.SpokePostureError, match="spoke-only"):
        m.check_spoke_posture(requested_role="primary", existing=None)


def test_check_spoke_posture_refuses_an_existing_primary_config():
    cfg = m.StackConfig(role="primary", instance_id="mini", primary_tailnet_address="x")
    with pytest.raises(m.SpokePostureError, match="primary"):
        m.check_spoke_posture(requested_role="spoke", existing=cfg)


def test_check_no_primary_env_refuses_a_hand_configured_primary_env_file():
    # ops/roam/env.primary.example's exact posture — this is the live hub's
    # own shape.
    text = "export SNOWLINE_INSTANCE_ID=primary\nexport SNOWLINE_PLATFORM_PORT=8848\n"
    with pytest.raises(m.SpokePostureError, match="primary"):
        m.check_no_primary_env({"platform": text})


def test_check_no_primary_env_accepts_a_roam_style_spoke_env_file():
    text = "export SNOWLINE_INSTANCE_ID=roam\n"
    m.check_no_primary_env({"platform": text})  # does not raise


def test_check_no_primary_env_accepts_no_existing_files():
    m.check_no_primary_env({})


# -- env-export parsing (drives both the primary guard and plist baking) ---


def test_parse_env_exports_handles_export_bare_and_quoted_values():
    text = (
        "# a comment\n"
        "\n"
        'export SNOWLINE_TRUSTED_CIDRS="100.64.0.0/10,127.0.0.0/8,::1"\n'
        "SNOWLINE_INSTANCE_ID=roam\n"
        "export SNOWLINE_REPLICATION_INTERVAL=30\n"
    )
    parsed = m.parse_env_exports(text)
    assert parsed == {
        "SNOWLINE_TRUSTED_CIDRS": "100.64.0.0/10,127.0.0.0/8,::1",
        "SNOWLINE_INSTANCE_ID": "roam",
        "SNOWLINE_REPLICATION_INTERVAL": "30",
    }


# -- migration-crossed detection --------------------------------------------


def test_migration_crossed_is_false_with_no_prior_venv():
    assert m.migration_crossed(None, ["headA"]) is False


def test_migration_crossed_is_false_when_heads_match():
    assert m.migration_crossed(["headA"], ["headA"]) is False


def test_migration_crossed_is_true_when_heads_differ():
    assert m.migration_crossed(["headA"], ["headB"]) is True


def test_any_migration_crossed_aggregates_per_service():
    assert m.any_migration_crossed({
        "platform": (["a"], ["a"]),
        "governance": (["a"], ["b"]),
    }) is True
    assert m.any_migration_crossed({"platform": (["a"], ["a"])}) is False


def test_head_probe_argv_uses_the_service_package_mapping():
    argv = m.head_probe_argv(Path("/venv/bin/python"), "governance")
    assert argv[0] == "/venv/bin/python"
    assert "snowline_governance" in argv[2]


def test_head_probe_argv_rejects_unknown_service():
    from pathlib import Path
    with pytest.raises(m.StackError, match="no package mapping"):
        m.head_probe_argv(Path("/x"), "walkthrough")


def test_parse_heads_reads_a_sorted_json_list():
    assert m.parse_heads('["b", "a"]') == ["b", "a"]
    assert m.parse_heads("") == []


# -- symlink swap planning + GC ---------------------------------------------


def test_plan_service_swap_detects_a_change():
    plan = m.plan_service_swap("platform", current_train="v0.1.0", target_train="v0.2.0")
    assert plan.changes is True
    assert plan.previous_train == "v0.1.0"


def test_plan_service_swap_no_change_when_train_matches():
    plan = m.plan_service_swap("platform", current_train="v0.2.0", target_train="v0.2.0")
    assert plan.changes is False


def test_plan_service_swap_fresh_install_has_no_previous_train():
    plan = m.plan_service_swap("platform", current_train=None, target_train="v0.1.0")
    assert plan.changes is True
    assert plan.previous_train is None


def test_trains_to_gc_keeps_only_the_named_set():
    doomed = m.trains_to_gc(["v0.1.0", "v0.2.0", "v0.3.0"], keep=["v0.2.0", "v0.3.0"])
    assert doomed == ["v0.1.0"]


def test_trains_to_gc_keeps_everything_named():
    assert m.trains_to_gc(["v0.1.0"], keep=["v0.1.0", "v0.2.0"]) == []


# -- env template drift (spec §5 step 3 / risk 5) ---------------------------


def test_plan_env_file_writes_when_absent():
    result = m.plan_env_file(
        service="platform", path=Path("/x/platform.env"),
        existing=None, rendered="export X=1\n",
    )
    assert result.action == "written"


def test_plan_env_file_unchanged_when_identical():
    result = m.plan_env_file(
        service="platform", path=Path("/x/platform.env"),
        existing="export X=1\n", rendered="export X=1\n",
    )
    assert result.action == "unchanged"


def test_plan_env_file_drifts_without_clobbering():
    result = m.plan_env_file(
        service="platform", path=Path("/x/platform.env"),
        existing="export X=1  # operator edit\n", rendered="export X=2\n",
    )
    assert result.action == "drifted"
    # The pure planner never writes anything itself — that's `sync.py`'s job,
    # gated on `action == "written"`, never on "drifted".


# -- plist rendering ---------------------------------------------------------


def test_render_plist_execs_current_bin_uvicorn_with_no_workingdirectory():
    from pathlib import Path
    xml = m.render_plist(
        service="governance",
        venv_current=Path("/home/x/Library/Application Support/Snowline/venvs/governance/current"),
        port=8801,
        env_file=Path("/home/x/.config/snowline/governance.env"),
        logs=Path("/home/x/logs"),
    )
    # sources the env file at exec time (#210 review) — never bakes values
    assert "<string>/bin/sh</string>" in xml
    assert "set -a; . '/home/x/.config/snowline/governance.env'; set +a; exec" in xml
    assert "venvs/governance/current/bin/uvicorn" in xml
    assert "snowline_governance.app:app" in xml
    assert "--port 8801" in xml
    assert "<key>WorkingDirectory</key>" not in xml
    assert "<key>EnvironmentVariables</key>" not in xml
    assert "dev.snowline.governance" in xml


def test_render_plist_rejects_unknown_service():
    from pathlib import Path
    with pytest.raises(m.StackError, match="no ASGI target"):
        m.render_plist(service="musher", venv_current=Path("/x"), port=1, env_file=Path("/e"), logs=Path("/l"))


def test_gui_target_shape():
    assert m.gui_target("dev.snowline.platform", 501) == "gui/501/dev.snowline.platform"


# -- auto-mode no-op + failure posture --------------------------------------


def test_is_noop_true_when_every_service_already_on_the_resolved_train():
    assert m.is_noop(
        resolved_train="v0.1.0",
        current_trains={"platform": "v0.1.0", "governance": "v0.1.0", "memory": "v0.1.0", "pm": "v0.1.0"},
    ) is True


def test_is_noop_false_on_a_fresh_install():
    assert m.is_noop(
        resolved_train="v0.1.0",
        current_trains={"platform": None, "governance": None, "memory": None, "pm": None},
    ) is False


def test_is_noop_false_when_one_service_is_behind():
    assert m.is_noop(
        resolved_train="v0.2.0",
        current_trains={"platform": "v0.2.0", "governance": "v0.1.0", "memory": "v0.2.0", "pm": "v0.2.0"},
    ) is False


def test_decide_post_upgrade_outcome_healthy_is_ok():
    assert m.decide_post_upgrade_outcome(health_ok=True, migration_crossed=True) == "ok"
    assert m.decide_post_upgrade_outcome(health_ok=True, migration_crossed=False) == "ok"


def test_decide_post_upgrade_outcome_unhealthy_no_migration_reverts():
    assert m.decide_post_upgrade_outcome(health_ok=False, migration_crossed=False) == "reverted"


def test_decide_post_upgrade_outcome_unhealthy_with_migration_never_auto_reverts():
    assert m.decide_post_upgrade_outcome(health_ok=False, migration_crossed=True) == "needs_attention"


# -- exit code contract (work item body: 0 = upgraded/current, else nonzero) --


@pytest.mark.parametrize("outcome,expected", [
    ("ok", 0),
    ("noop", 0),
    ("reverted", 1),
    ("needs_attention", 1),
    ("failed", 1),
])
def test_exit_code_contract(outcome, expected):
    assert m.exit_code(outcome) == expected


# -- run report ---------------------------------------------------------


def test_run_report_round_trips_service_changes_through_json():
    report = m.RunReport(
        train="v0.2.0", role="spoke", instance_id="roam",
        started_at="2026-09-01T07:00:00+00:00", finished_at="2026-09-01T07:00:05+00:00",
        auto=True, dry_run=False, noop=False,
        services=(
            m.ServiceChange(service="platform", previous_train="v0.1.0", target_train="v0.2.0",
                             changed=True, migration_crossed=False, kickstarted=True),
        ),
        health_ok=True, outcome="ok",
    )
    data = json.loads(report.to_json())
    assert data["train"] == "v0.2.0"
    assert data["auto"] is True
    assert data["services"][0]["kickstarted"] is True
    assert data["services"][0]["migration_crossed"] is False


def test_parse_env_exports_strips_unquoted_inline_comments():
    """`export SNOWLINE_INSTANCE_ID=primary # hub` must parse as 'primary' —
    keeping the comment text let a commented primary declaration slip past
    the spoke-only guard (#210 review). Quoted values keep their content."""
    parsed = m.parse_env_exports(
        "export SNOWLINE_INSTANCE_ID=primary # the hub\n"
        'export SNOWLINE_PM_ROLE="spoke"  # quoted\n'
        "export KEEP_HASH='a#b'\n"
    )
    assert parsed["SNOWLINE_INSTANCE_ID"] == "primary"
    assert parsed["SNOWLINE_PM_ROLE"] == "spoke"
    assert parsed["KEEP_HASH"] == "a#b"


# ==========================================================================
# `snowline stack bootstrap-spoke` (item 71317cd6 / snowline-pm#122)
# ==========================================================================


# -- stack.json additive migration --------------------------------------


def test_stack_config_old_shape_loads_with_bootstrap_fields_none():
    """A stack.json written by `sync` alone (pre-bootstrap-spoke) has neither
    additive field — that must stay a loadable file, not a schema error
    (spec §6: "missing key = prompt", not "reject the file")."""
    cfg = m.parse_stack_config(
        {"schema_version": 1, "role": "spoke", "instance_id": "roam",
         "primary_tailnet_address": "mini.ts.net"}
    )
    assert cfg.primary_gateway_url is None
    assert cfg.local_tailnet_address is None
    # round-trips without emitting the missing keys as nulls
    assert "primary_gateway_url" not in json.loads(cfg.to_json())


def test_stack_config_new_shape_round_trips_bootstrap_fields():
    cfg = m.StackConfig(
        role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net",
        primary_gateway_url="http://mini.ts.net:8850", local_tailnet_address="roam.ts.net",
    )
    data = json.loads(cfg.to_json())
    assert data["primary_gateway_url"] == "http://mini.ts.net:8850"
    assert data["local_tailnet_address"] == "roam.ts.net"
    reloaded = m.parse_stack_config(data)
    assert reloaded == cfg


def test_default_primary_gateway_url_uses_the_hub_real_port_8850_not_8848():
    """The hub's real gateway port is :8850 — NOT the roam runbook's
    illustrative :8848 (that's SERVICE_PORTS["platform"], the SPOKE's own
    port)."""
    assert m.default_primary_gateway_url("mini.tailnet.ts.net") == "http://mini.tailnet.ts.net:8850"


# -- preconditions --------------------------------------------------------


def test_check_bootstrap_stack_config_refuses_when_missing():
    with pytest.raises(m.StackError, match="run `snowline stack sync` first"):
        m.check_bootstrap_stack_config(None)


def test_check_bootstrap_stack_config_passes_through_when_present():
    cfg = m.StackConfig(role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net")
    assert m.check_bootstrap_stack_config(cfg) is cfg


def test_check_local_services_installed_refuses_when_any_missing():
    trains = {"platform": "v0.2.0", "governance": None, "memory": "v0.2.0", "pm": "v0.2.0"}
    with pytest.raises(m.StackError, match="governance.*run `snowline stack sync` first"):
        m.check_local_services_installed(trains)


def test_check_local_services_installed_passes_when_all_present():
    trains = {"platform": "v0.2.0", "governance": "v0.2.0", "memory": "v0.2.0", "pm": "v0.2.0"}
    m.check_local_services_installed(trains)  # does not raise


def test_check_local_gateway_healthy_refuses_when_unhealthy():
    with pytest.raises(m.StackError, match="local gateway is not healthy"):
        m.check_local_gateway_healthy(False)


def test_check_primary_gateway_healthy_refuses_when_unreachable():
    with pytest.raises(m.StackError, match="primary's gateway.*not reachable"):
        m.check_primary_gateway_healthy(False, "http://mini.ts.net:8850")


def test_check_pm_role_is_spoke_refuses_when_missing_file():
    with pytest.raises(m.StackError, match="pm.env does not exist"):
        m.check_pm_role_is_spoke(None)


def test_check_pm_role_is_spoke_refuses_loudly_on_any_other_role():
    with pytest.raises(m.StackError, match="SNOWLINE_PM_ROLE='primary'"):
        m.check_pm_role_is_spoke("export SNOWLINE_PM_ROLE=primary\n")


def test_check_pm_role_is_spoke_refuses_when_unset():
    with pytest.raises(m.StackError, match="SNOWLINE_PM_ROLE=None"):
        m.check_pm_role_is_spoke("export SNOWLINE_INSTANCE_ID=roam\n")


def test_check_pm_role_is_spoke_passes_when_declared_spoke():
    m.check_pm_role_is_spoke("export SNOWLINE_PM_ROLE=spoke\n")  # does not raise


# -- seed-config construction ---------------------------------------------


def test_build_seed_config_shapes_participants_from_stack_config():
    cfg = m.StackConfig(
        role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net",
        primary_gateway_url="http://mini.ts.net:8850", local_tailnet_address="roam.ts.net",
    )
    config = m.build_seed_config(cfg, local_platform_port=8848, pg_user="sean")
    assert config["primary"] == {"platform_url": "http://mini.ts.net:8850", "instance": "primary"}
    assert config["spoke"] == {"platform_url": "http://127.0.0.1:8848", "instance": "roam"}
    assert set(config["participants"]) == {"platform", "governance", "memory", "pm"}
    platform_p = config["participants"]["platform"]
    assert platform_p["spoke_ingest_url"] == "http://roam.ts.net:8848/replication/events/ingest"
    assert platform_p["primary_dump_url"] == "postgresql://sean@mini.ts.net:5432/snowline_platform"
    assert platform_p["spoke_db_url"] == "postgresql:///snowline_platform"
    pm_p = config["participants"]["pm"]
    assert pm_p["spoke_ingest_url"] == "http://roam.ts.net:8803/events/ingest"
    assert pm_p["primary_dump_url"] == "postgresql://sean@mini.ts.net:5432/snowline_pm"
    governance_p = config["participants"]["governance"]
    assert governance_p["spoke_ingest_url"] == "http://roam.ts.net:8801/events/ingest"


def test_build_seed_config_refuses_without_the_additive_fields():
    cfg = m.StackConfig(role="spoke", instance_id="roam", primary_tailnet_address="mini.ts.net")
    with pytest.raises(m.StackError, match="missing primary_gateway_url"):
        m.build_seed_config(cfg, local_platform_port=8848, pg_user="sean")


# -- CLI argv builders (the fake-runner orchestration tests assert these
# exactly, but the builders themselves are pure and independently checked) --


def test_seed_cli_argv_plain():
    path = Path("/home/roam/.config/snowline/seed.json")
    assert m.seed_cli_argv(path) == ["snowline", "replicate", "seed", "--config", str(path)]


def test_seed_cli_argv_reverse_pair():
    path = Path("/x/seed.json")
    assert m.seed_cli_argv(path, reverse_pair=True) == [
        "snowline", "replicate", "seed", "--config", str(path), "--reverse-pair",
    ]


def test_seed_cli_argv_reseed():
    path = Path("/x/seed.json")
    assert m.seed_cli_argv(path, reseed=True) == [
        "snowline", "replicate", "seed", "--config", str(path), "--reseed",
    ]


def test_reseed_check_cli_argv():
    path = Path("/x/seed.json")
    assert m.reseed_check_cli_argv(path) == [
        "snowline", "replicate", "reseed-check", "--config", str(path),
    ]


def test_bootstrap_exit_code_contract():
    assert m.bootstrap_exit_code("ok") == 0
    assert m.bootstrap_exit_code("failed") == 1


def test_bootstrap_report_round_trips_steps_through_json():
    report = m.BootstrapReport(
        instance_id="roam", primary_gateway_url="http://mini.ts.net:8850",
        started_at="2026-09-01T07:00:00+00:00", finished_at="2026-09-01T07:00:05+00:00",
        dry_run=False, reseed=False,
        steps=(m.BootstrapStep(name="seed", status="ok", detail="primed, dumped, scrubbed"),),
        pm_role_ok=True, health_ok=True, outcome="ok",
    )
    data = json.loads(report.to_json())
    assert data["instance_id"] == "roam"
    assert data["steps"][0]["name"] == "seed"
    assert data["pm_role_ok"] is True
