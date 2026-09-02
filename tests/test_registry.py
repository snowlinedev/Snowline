"""Plugin registry + manifest behavior."""

import pytest
from pydantic import ValidationError

from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import (
    HttpPrefixConflict,
    PluginNotFound,
    PluginRegistry,
    PluginStatus,
    RegisteredPlugin,
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


# --- http prefixes (gateway.md §3a) ------------------------------------------
#
# The registry owns the cross-plugin invariant: root-level http prefixes are a
# shared namespace, so two plugins can never hold colliding ones, and
# resolution of a request path to a plugin lives here (not in the route).


def _http(name, *prefixes, base_url="http://127.0.0.1:8802") -> PluginManifest:
    return PluginManifest(
        name=name,
        base_url=base_url,
        http=[{"prefix": p, "methods": ["GET", "POST"]} for p in prefixes],
    )


def test_http_route_resolves_the_declaring_plugin():
    reg = PluginRegistry()
    reg.upsert(_http("pm", "/provider"))
    entry, surface = reg.http_route("/provider/work-items")
    assert entry.manifest.name == "pm"
    assert surface.prefix == "/provider"
    # The prefix itself resolves too, not only paths under it.
    assert reg.http_route("/provider")[1].prefix == "/provider"


def test_http_route_is_none_for_an_unclaimed_path():
    reg = PluginRegistry()
    reg.upsert(_http("pm", "/provider"))
    assert reg.http_route("/nope") is None
    assert reg.http_route("/") is None


def test_http_route_is_segment_aligned():
    # '/providerx' must NOT match '/provider' — a string-prefix match would
    # hand one plugin every path that happens to share its opening characters.
    reg = PluginRegistry()
    reg.upsert(_http("pm", "/provider"))
    assert reg.http_route("/providerx") is None
    assert reg.http_route("/providerx/work-items") is None


def test_http_route_picks_the_longest_prefix():
    # Two plugins can't hold nested prefixes (that's the collision rule), so
    # this pins the resolution rule via a registry seeded directly.
    reg = PluginRegistry()
    reg.upsert(_http("pm", "/provider"))
    reg._plugins["deep"] = RegisteredPlugin(
        manifest=_http("deep", "/provider/work-items", base_url="http://x:1")
    )
    assert reg.http_route("/provider/work-items/7")[0].manifest.name == "deep"
    assert reg.http_route("/provider/other")[0].manifest.name == "pm"


def test_http_routes_lists_every_prefix_sorted():
    reg = PluginRegistry()
    reg.upsert(_http("pm", "/provider"))
    reg.upsert(_http("gov", "/attest", base_url="http://127.0.0.1:8801"))
    assert [(n, s.prefix) for n, s in reg.http_routes()] == [
        ("gov", "/attest"),
        ("pm", "/provider"),
    ]


@pytest.mark.parametrize(
    "claimed", ["/provider", "/provider/work-items", "/provider/work-items/x"]
)
def test_upsert_refuses_a_prefix_another_plugin_holds(claimed):
    # Collision = equal OR containment, in either direction.
    reg = PluginRegistry()
    reg.upsert(_http("pm", "/provider/work-items"))
    with pytest.raises(HttpPrefixConflict) as exc:
        reg.upsert(_http("impostor", claimed, base_url="http://x:1"))
    assert exc.value.prefix == claimed
    assert exc.value.holder == "pm"
    assert exc.value.holder_prefix == "/provider/work-items"
    # The refused plugin is not registered at all — the whole upsert is
    # refused, not partially applied.
    with pytest.raises(PluginNotFound):
        reg.get("impostor")


def test_upsert_allows_a_disjoint_prefix_from_another_plugin():
    reg = PluginRegistry()
    reg.upsert(_http("pm", "/provider"))
    reg.upsert(_http("gov", "/attest", base_url="http://x:1"))
    assert {e.manifest.name for e in reg.list()} == {"pm", "gov"}


def test_same_plugin_may_heartbeat_and_reshape_its_own_prefixes():
    reg = PluginRegistry()
    reg.upsert(_http("pm", "/provider"))
    # The heartbeat: an identical manifest is `unchanged`, not a self-collision.
    assert reg.upsert(_http("pm", "/provider"))[1] == "unchanged"
    # A redeploy that nests its own prefix is fine too — only ANOTHER plugin's
    # claim refuses.
    _, outcome = reg.upsert(_http("pm", "/provider/work-items"))
    assert outcome == "updated"
    assert reg.http_route("/provider") is None
    assert reg.http_route("/provider/work-items")[0].manifest.name == "pm"


def test_unregister_frees_the_prefix():
    reg = PluginRegistry()
    reg.upsert(_http("pm", "/provider"))
    with pytest.raises(HttpPrefixConflict):
        reg.upsert(_http("other", "/provider", base_url="http://x:1"))
    reg.unregister("pm")
    reg.upsert(_http("other", "/provider", base_url="http://x:1"))
    assert reg.http_route("/provider")[0].manifest.name == "other"
