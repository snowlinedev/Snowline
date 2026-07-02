"""Plugin registry + manifest behavior."""

import pytest
from pydantic import ValidationError

from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import (
    PluginNotFound,
    PluginRegistry,
    PluginStatus,
)


def _manifest(name="governance", base_url="http://127.0.0.1:8801", **kw) -> PluginManifest:
    return PluginManifest(name=name, base_url=base_url, **kw)


# --- manifest validation -----------------------------------------------------

def test_manifest_defaults():
    m = _manifest()
    assert m.mcp_path == "/mcp"
    assert m.health_path == "/health"
    assert m.ui_path is None
    assert m.scopes == []


def test_manifest_strips_trailing_slash_on_base_url():
    assert _manifest(base_url="http://x:8801/").base_url == "http://x:8801"


def test_manifest_rejects_bad_name():
    with pytest.raises(ValidationError):
        _manifest(name="Bad Name")  # space + uppercase


def test_manifest_rejects_non_http_base_url():
    with pytest.raises(ValidationError):
        PluginManifest(name="governance", base_url="ftp://nope")


# --- registry ----------------------------------------------------------------

def test_upsert_and_get_and_list():
    reg = PluginRegistry()
    entry, outcome = reg.upsert(_manifest())
    assert outcome == "created"
    assert entry.status is PluginStatus.UNKNOWN
    assert reg.get("governance").manifest.name == "governance"
    assert [e.manifest.name for e in reg.list()] == ["governance"]


def test_upsert_creates_then_is_idempotent():
    reg = PluginRegistry()
    entry, outcome = reg.upsert(_manifest())
    assert outcome == "created"
    assert entry.status is PluginStatus.UNKNOWN
    # Re-upserting an IDENTICAL manifest keeps the entry — including its health
    # status, so a heartbeat can't flap a plugin back to UNKNOWN every beat.
    reg.set_status("governance", PluginStatus.UP)
    entry2, outcome2 = reg.upsert(_manifest())
    assert outcome2 == "unchanged"
    assert entry2 is reg.get("governance")
    assert entry2.status is PluginStatus.UP


def test_upsert_replaces_on_changed_manifest():
    reg = PluginRegistry()
    reg.upsert(_manifest())
    reg.set_status("governance", PluginStatus.UP)
    # A different manifest (a redeploy moved the plugin) replaces the entry and
    # resets status — the old UP described a plugin at another address.
    entry, outcome = reg.upsert(_manifest(base_url="http://127.0.0.1:9999"))
    assert outcome == "updated"
    assert entry.manifest.base_url == "http://127.0.0.1:9999"
    assert entry.status is PluginStatus.UNKNOWN


def test_get_and_unregister_missing_raise():
    reg = PluginRegistry()
    with pytest.raises(PluginNotFound):
        reg.get("nope")
    with pytest.raises(PluginNotFound):
        reg.unregister("nope")


def test_unregister_removes():
    reg = PluginRegistry()
    reg.upsert(_manifest())
    reg.unregister("governance")
    assert reg.list() == []


def test_set_status_updates_and_is_noop_for_missing():
    reg = PluginRegistry()
    reg.upsert(_manifest())
    reg.set_status("governance", PluginStatus.UP)
    assert reg.get("governance").status is PluginStatus.UP
    reg.set_status("ghost", PluginStatus.UP)  # no-op, no raise


def test_set_status_is_noop_for_replaced_entry():
    # The health poller pins its write to the entry it probed: a result for an
    # entry that an `updated` upsert replaced mid-round must not mark the new
    # entry (the new address was never checked).
    reg = PluginRegistry()
    old_entry, _ = reg.upsert(_manifest())
    new_entry, outcome = reg.upsert(_manifest(base_url="http://127.0.0.1:9999"))
    assert outcome == "updated"
    reg.set_status("governance", PluginStatus.DOWN, expected_entry=old_entry)
    assert reg.get("governance").status is PluginStatus.UNKNOWN  # untouched
    reg.set_status("governance", PluginStatus.UP, expected_entry=new_entry)
    assert reg.get("governance").status is PluginStatus.UP
