"""`snowline_plugin_sdk.train` — which release train a process runs (#259)."""

from __future__ import annotations

import json
from importlib import metadata

import pytest
from snowline_plugin_sdk import train


class _Dist:
    def __init__(self, version, direct_url=None):
        self.version = version
        self._direct = direct_url

    def read_text(self, name):
        assert name == "direct_url.json"
        return json.dumps(self._direct) if self._direct is not None else None


@pytest.mark.parametrize(
    "raw,parsed",
    [("v0.5.1", (0, 5, 1)), ("0.5.12", (0, 5, 12)), ("source", None),
     (None, None), ("0.5.1rc1", None), ("", None)],
)
def test_parse_train(raw, parsed):
    assert train.parse_train(raw) == parsed


def test_wheel_install_reports_its_train(monkeypatch):
    monkeypatch.setattr(
        train.metadata, "distribution",
        lambda name: _Dist("0.5.1", {"url": "file:///x/snowline_platform-0.5.1-py3-none-any.whl",
                                     "archive_info": {}}),
    )
    info = train.running_train("snowline-platform")
    assert info == {
        "version": "v0.5.1",
        "package": "snowline-platform",
        "package_version": "0.5.1",
        "editable": False,
        "git_sha": None,
    }


def test_wheel_without_direct_url_reports_its_train(monkeypatch):
    monkeypatch.setattr(train.metadata, "distribution", lambda name: _Dist("0.6.0"))
    assert train.running_train("x")["version"] == "v0.6.0"


def test_editable_checkout_reports_source_and_git_sha(monkeypatch, tmp_path):
    monkeypatch.setattr(
        train.metadata, "distribution",
        lambda name: _Dist("0.0.1", {"url": f"file://{tmp_path}", "dir_info": {"editable": True}}),
    )
    seen = {}

    def fake_sha(path):
        seen["path"] = path
        return "a" * 40

    monkeypatch.setattr(train, "git_sha", fake_sha)
    info = train.running_train("snowline-platform")
    assert info["version"] == train.SOURCE
    assert info["editable"] is True
    assert info["git_sha"] == "a" * 40
    assert info["package_version"] == "0.0.1"
    assert seen["path"] == tmp_path


def test_missing_distribution_reports_none(monkeypatch):
    def boom(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(train.metadata, "distribution", boom)
    info = train.running_train("nope")
    assert info["version"] is None and info["package_version"] is None


def test_non_train_version_reports_none(monkeypatch):
    monkeypatch.setattr(train.metadata, "distribution", lambda name: _Dist("0.0.1.dev3"))
    assert train.running_train("x")["version"] is None


def test_git_sha_of_a_non_checkout_is_none(tmp_path):
    assert train.git_sha(tmp_path) is None


def test_this_workspace_runs_from_source():
    """The test env installs the workspace editable — the real metadata path."""
    info = train.running_train("snowline-plugin-sdk")
    assert info["editable"] is True
    assert info["version"] == train.SOURCE
