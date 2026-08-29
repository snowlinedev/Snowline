"""The pure release logic (issue #202): planning, respin carry-forward, version
stamping, the SDK-pin rewrite, preflight refusals, tag decisions, notes.

Everything here runs without a checkout, a network, or a `gh` token — that is
the point of keeping `release/model.py` side-effect free. The orchestration on
top of it is covered by test_release_cut.py against a fake runner; the
end-to-end cut is an operator action, not a test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snowline_platform.release import model as m

CONFIG_PATH = Path(__file__).resolve().parents[1] / "release" / "components.json"


@pytest.fixture()
def config() -> m.ReleaseConfig:
    return m.load_config(CONFIG_PATH)


HEADS = {"platform": "a" * 40, "pm": "b" * 40}


# -- the checked-in config ------------------------------------------------


def test_checked_in_config_matches_the_spec_component_set(config):
    assert [c.name for c in config.components] == ["platform", "pm"]
    assert [s.name for _, s in config.services()] == [
        "platform", "governance", "memory", "sdk", "pm",
    ]
    # §2: remote-front is a workspace member that never ships.
    assert config.component("platform").prune_wheels == ("snowline_remote_front",)
    # §2.2: the dashboard rides the platform component.
    assert config.component("platform").dashboard is not None
    # §4: the platform release carries the manifest.
    assert config.manifest_component.name == "platform"
    # Risk #6 is armed on pm and only on pm.
    assert config.component("pm").rewrite_sdk_pin is True
    assert config.component("platform").rewrite_sdk_pin is False


def test_every_service_kind_service_can_be_smoke_booted(config):
    """§2.1 covers EVERY service, so a service without a boot spec is a hole."""
    for _, svc in config.services():
        if svc.is_service:
            assert svc.boot is not None, svc.name
            assert svc.boot.database_url_env.startswith("SNOWLINE_")
    assert config.component("platform").service("sdk").kind == "library"


def test_config_rejects_duplicate_service_names():
    data = {
        "components": [
            {"name": "a", "repo": "o/a", "path": ".", "services": [{"name": "x", "package": "x"}]},
            {"name": "b", "repo": "o/b", "path": ".", "services": [{"name": "x", "package": "y"}]},
        ]
    }
    with pytest.raises(m.ReleaseError, match="duplicate service names"):
        m.parse_config(data)


# -- versions -------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0.1.0", "v1.0.0", "v0.1", "v0.1.0-rc1", "latest"])
def test_version_must_be_the_train_scheme(bad):
    with pytest.raises(m.ReleaseError):
        m.validate_version(bad)


def test_wheel_filenames_drop_the_v_and_normalize_the_name():
    assert m.wheel_filename("snowline-plugin-sdk", "v0.1.0") == \
        "snowline_plugin_sdk-0.1.0-py3-none-any.whl"


# -- planning -------------------------------------------------------------


def test_minor_cut_tags_every_component_at_head(config):
    plan = m.plan_train(config, "v0.1.0", heads=HEADS)
    assert {p.tag for p in plan.services} == {"v0.1.0"}
    assert plan.components_to_build == ("platform", "pm")
    manifest = plan.to_manifest()
    assert manifest["version"] == "v0.1.0"
    assert manifest["components"]["governance"] == {
        "repo": "snowlinedev/Snowline",
        "tag": "v0.1.0",
        "sha": HEADS["platform"],
        "wheel": "snowline_governance-0.1.0-py3-none-any.whl",
        "kind": "service",
    }
    # The three platform services share a repo, so they share a sha and a tag.
    assert manifest["components"]["platform"]["sha"] == manifest["components"]["memory"]["sha"]
    assert manifest["components"]["pm"]["repo"] == "snowlinedev/snowline-pm"


def test_respin_retags_only_that_component_and_carries_the_rest_verbatim(config):
    previous = m.plan_train(config, "v0.1.0", heads=HEADS).to_manifest()
    moved = {"platform": "a" * 40, "pm": "c" * 40}
    plan = m.plan_train(config, "v0.1.1", heads=moved, previous=previous, respin="pm")

    assert plan.components_to_build == ("pm",)
    entries = plan.to_manifest()["components"]
    # Only pm moved.
    assert entries["pm"]["tag"] == "v0.1.1"
    assert entries["pm"]["sha"] == "c" * 40
    assert entries["pm"]["wheel"] == "snowline_pm-0.1.1-py3-none-any.whl"
    # Everything else keeps the v0.1.0 tag AND the v0.1.0 wheel — sync fetches
    # each component from its manifest-recorded tag (spec §4).
    for name in ("platform", "governance", "memory", "sdk"):
        assert entries[name]["tag"] == "v0.1.0", name
        assert "0.1.0" in entries[name]["wheel"], name
    assert not any(p.rebuilt for p in plan.services if p.component == "platform")


def test_respin_of_the_platform_moves_all_four_of_its_services(config):
    previous = m.plan_train(config, "v0.1.0", heads=HEADS).to_manifest()
    moved = {"platform": "d" * 40, "pm": "b" * 40}
    plan = m.plan_train(config, "v0.1.1", heads=moved, previous=previous, respin="platform")
    entries = plan.to_manifest()["components"]
    for name in ("platform", "governance", "memory", "sdk"):
        assert entries[name]["tag"] == "v0.1.1"
        assert entries[name]["sha"] == "d" * 40
    assert entries["pm"]["tag"] == "v0.1.0"


def test_respin_without_a_previous_train_is_refused(config):
    with pytest.raises(m.ReleaseError, match="needs an existing release/train.json"):
        m.plan_train(config, "v0.1.1", heads=HEADS, respin="pm")


def test_respin_of_an_unknown_component_is_refused(config):
    previous = m.plan_train(config, "v0.1.0", heads=HEADS).to_manifest()
    with pytest.raises(m.ReleaseError, match="unknown component 'musher'"):
        m.plan_train(config, "v0.1.1", heads=HEADS, previous=previous, respin="musher")


def test_recutting_the_same_version_at_the_same_shas_is_allowed(config):
    """The resume path: a cut that tagged but died before publishing."""
    previous = m.plan_train(config, "v0.1.0", heads=HEADS).to_manifest()
    plan = m.plan_train(config, "v0.1.0", heads=HEADS, previous=previous)
    assert plan.to_manifest() == previous


def test_recutting_the_same_version_at_a_different_sha_is_refused(config):
    previous = m.plan_train(config, "v0.1.0", heads=HEADS).to_manifest()
    with pytest.raises(m.ReleaseError, match="already recorded"):
        m.plan_train(config, "v0.1.0", heads={"platform": "e" * 40, "pm": "b" * 40},
                     previous=previous)


def test_manifest_round_trips_through_json(config, tmp_path):
    plan = m.plan_train(config, "v0.1.0", heads=HEADS)
    path = tmp_path / "train.json"
    path.write_text(m.render_manifest(plan))
    assert json.loads(path.read_text()) == plan.to_manifest()
    assert m.load_manifest(tmp_path / "absent.json") is None


# -- version stamping -----------------------------------------------------

PYPROJECT = """\
[project]
name = "snowline-pm"
version = "0.0.1"
requires-python = ">=3.12"

[tool.hatch.build.targets.wheel]
packages = ["src/snowline_pm"]
version = "not-this-one"
"""


def test_stamp_rewrites_only_the_project_version():
    out = m.stamp_pyproject_version(PYPROJECT, "v0.4.2")
    assert '\nversion = "0.4.2"\n' in out
    # The `[tool.*]` table's key is untouched.
    assert 'version = "not-this-one"' in out
    assert out.count('version = "0.4.2"') == 1


def test_stamp_refuses_a_pyproject_without_a_project_version():
    with pytest.raises(m.ReleaseError, match="no `version"):
        m.stamp_pyproject_version('[project]\nname = "x"\n', "v0.1.0")
    with pytest.raises(m.ReleaseError, match="no \\[project\\] table"):
        m.stamp_pyproject_version('[tool.x]\nversion = "1"\n', "v0.1.0")


# -- the SDK-pin rewrite (spec risk #6) -----------------------------------

PM_EXPORT = """\
# This file was autogenerated by uv via the following command:
#    uv export --frozen --no-emit-workspace
httpx==0.28.1 \\
    --hash=sha256:aaa \\
    --hash=sha256:bbb
    # via snowline-pm
snowline-plugin-sdk @ git+https://github.com/snowlinedev/Snowline.git@8adaeb3774041efda8e8fae9d05c12ef77d4abea#subdirectory=sdk
    # via snowline-pm
sqlalchemy==2.0.52 \\
    --hash=sha256:ccc
    # via snowline-pm
"""


def test_sdk_git_pin_becomes_the_trains_wheel_pin():
    result = m.rewrite_sdk_pin(PM_EXPORT, "v0.1.0")
    assert result.found and result.was_git_pin
    assert result.git_rev == "8adaeb3774041efda8e8fae9d05c12ef77d4abea"
    assert "git+" not in result.text
    assert "snowline-plugin-sdk==0.1.0\n" in result.text
    # The provenance comment survives; the neighbours are untouched.
    assert "    # via snowline-pm" in result.text
    assert "httpx==0.28.1 \\" in result.text
    assert result.text.count("--hash=sha256:ccc") == 1
    assert result.text.endswith("\n")


def test_sdk_rewrite_leaves_every_other_requirement_alone():
    result = m.rewrite_sdk_pin(PM_EXPORT, "v0.1.0")
    before = [ln for ln in PM_EXPORT.splitlines() if "plugin-sdk" not in ln]
    after = [ln for ln in result.text.splitlines() if "plugin-sdk" not in ln]
    assert before == after


def test_sdk_rewrite_preserves_an_environment_marker():
    text = "snowline-plugin-sdk @ git+https://x/y.git@abc1234 ; python_version >= '3.12'\n"
    result = m.rewrite_sdk_pin(text, "v0.2.0")
    assert result.text == "snowline-plugin-sdk==0.2.0 ; python_version >= '3.12'\n"


def test_sdk_rewrite_repins_an_already_versioned_dependency():
    """The spec's other option for risk #6 — pm moving to a versioned SDK dep.
    The rewrite still has to land the TRAIN's version, not the locked one."""
    text = "snowline-plugin-sdk==0.0.9\n    # via snowline-pm\n"
    result = m.rewrite_sdk_pin(text, "v0.3.0")
    assert result.found and not result.was_git_pin
    assert "snowline-plugin-sdk==0.3.0" in result.text


def test_sdk_rewrite_reports_a_miss_rather_than_silently_passing():
    result = m.rewrite_sdk_pin("httpx==0.28.1\n", "v0.1.0")
    assert not result.found
    assert result.text == "httpx==0.28.1\n"


def test_sdk_rewrite_does_not_match_a_similarly_named_package():
    text = "snowline-plugin-sdk-extras==1.0\n"
    assert not m.rewrite_sdk_pin(text, "v0.1.0").found


# -- preflight ------------------------------------------------------------


def _state(**kw) -> m.CheckoutState:
    base = {
        "component": "pm", "path": Path("/tmp/pm"), "exists": True, "is_git": True,
        "branch": "main", "dirty": False, "head": "a" * 40, "origin_head": "a" * 40,
        "head_pushed": True,
    }
    base.update(kw)
    return m.CheckoutState(**base)


def test_clean_pushed_main_passes_preflight():
    assert m.preflight_issues(_state()) == ()
    assert m.preflight_warnings(_state()) == ()


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"exists": False}, "checkout missing"),
        ({"is_git": False}, "not a git checkout"),
        ({"branch": "feature/x"}, "expected main"),
        ({"dirty": True}, "dirty"),
        ({"head_pushed": False}, "not on origin/main"),
    ],
)
def test_preflight_refuses_anything_that_would_make_a_tag_lie(kwargs, expected):
    issues = m.preflight_issues(_state(**kwargs))
    assert any(expected in issue for issue in issues), issues


def test_being_behind_origin_warns_but_does_not_refuse():
    state = _state(head="a" * 40, origin_head="f" * 40)
    assert m.preflight_issues(state) == ()
    assert "behind origin/main" in m.preflight_warnings(state)[0]


# -- tag idempotency ------------------------------------------------------


def test_tag_decisions():
    assert m.tag_decision(tag="v0.1.0", target_sha="a" * 40, existing_sha=None) == "create"
    assert m.tag_decision(tag="v0.1.0", target_sha="a" * 40, existing_sha="a" * 40) == "skip"
    with pytest.raises(m.ReleaseError, match="Refusing to move a published tag"):
        m.tag_decision(tag="v0.1.0", target_sha="a" * 40, existing_sha="b" * 40)


# -- release notes --------------------------------------------------------


def test_release_notes_list_merged_prs_since_the_previous_tag():
    notes = m.format_release_notes(
        component="pm", repo="snowlinedev/snowline-pm", version="v0.1.1",
        previous_tag="v0.1.0",
        subjects=["Provider endpoints (#128)", "Dispatch opt-in flag (#124)"],
        assets=["snowline_pm-0.1.1-py3-none-any.whl"],
    )
    assert "Changes since `v0.1.0`" in notes
    assert "- Provider endpoints (#128)" in notes
    assert "`snowline_pm-0.1.1-py3-none-any.whl`" in notes


def test_release_notes_say_so_when_there_is_no_previous_tag():
    notes = m.format_release_notes(
        component="platform", repo="snowlinedev/Snowline", version="v0.1.0",
        previous_tag=None, subjects=["First (#1)"],
    )
    assert "First train tag on this repo" in notes


def test_release_notes_handle_a_respin_with_no_new_commits():
    notes = m.format_release_notes(
        component="pm", repo="snowlinedev/snowline-pm", version="v0.1.1",
        previous_tag="v0.1.0", subjects=[],
    )
    assert "No commits since the previous tag" in notes
