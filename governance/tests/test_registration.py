"""Governance's PLUGIN-SPECIFIC registration assertions — the manifest it posts
(surfaces + the shadow-discussions `ui` block) and that its `registration_heartbeat`
wrapper wires into the shared SDK loop.

The heartbeat MECHANISM (retry, 409-as-success, non-finite interval guard,
first-beat INFO, the httpx log filter) is tested ONCE in the SDK
(`sdk/tests/test_registration.py`, issue #50). The ~30-line loop harness is
imported from `snowline_plugin_sdk.testing` rather than redefined here.

Stubs the platform HTTP with an `httpx.MockTransport`, so no platform runs.
"""

from __future__ import annotations

import json

import httpx
from snowline_plugin_sdk.testing import mock_client, run_heartbeat_until

from snowline_governance import registration


def test_register_posts_the_right_manifest():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"name": "governance", "status": "unknown"})

    ok = registration.register_with_platform(
        platform_url="http://platform.example",
        base_url="http://gov.example:8801",
        client=mock_client(handler),
    )
    assert ok is True
    assert captured["url"] == "http://platform.example/plugins"
    m = captured["json"]
    assert m["name"] == "governance"
    assert m["base_url"] == "http://gov.example:8801"
    assert m["mcp_path"] == "/mcp"
    assert m["health_path"] == "/health"
    # The surface mapping: /mcp -> the platform's `main` named surface.
    assert m["surfaces"] == {"/mcp": "main", "/shadow/mcp": "shadow"}
    # The `ui` block (ui-shell.md §3, issue #55) — governance's shadow-
    # discussions contribution: one `stat` widget + two pages (`table` list,
    # `thread` detail keyed on the branch id).
    ui = m["ui"]
    assert ui["contract_version"] == 1
    assert [w["id"] for w in ui["widgets"]] == [
        "shadow-activity",
        "unreconciled-decisions",
    ]
    widget = ui["widgets"][0]
    assert widget["slot"] == "home"
    assert widget["kind"] == "stat"
    assert widget["data"] == "/ui-api/widgets/shadow-activity"
    assert widget["refresh_seconds"] == 30
    # The §6.1 unreconciled-pairs stat (replication-continuity, #79).
    unreconciled = ui["widgets"][1]
    assert unreconciled["slot"] == "home"
    assert unreconciled["kind"] == "stat"
    assert unreconciled["data"] == "/ui-api/widgets/unreconciled-decisions"
    # The replication opt-in block (replication-continuity §4, #79): advisory
    # metadata the pairing step reads — contract version + ingest route + the
    # FULL drift-guarded event vocabulary.
    from snowline_governance.contract import CONTRACT_VERSION, EVENT_TYPES

    rep = m["replication"]
    assert rep["contract_version"] == CONTRACT_VERSION
    assert rep["ingest_path"] == "/events/ingest"
    assert rep["events"] == sorted(EVENT_TYPES)
    assert [p["id"] for p in ui["pages"]] == ["shadow-branches", "shadow-branch"]
    table_page, thread_page = ui["pages"]
    assert table_page["route"] == "/shadow"
    assert table_page["nav"] is True
    assert table_page["kind"] == "table"
    assert table_page["data"] == "/ui-api/pages/branches"
    assert thread_page["route"] == "/shadow/{branch_id}"
    assert thread_page["nav"] is False
    assert thread_page["kind"] == "thread"
    assert thread_page["data"] == "/ui-api/pages/branches/{branch_id}"
    # The composer write seam (shadow-conversations §4/§5): its endpoint's
    # `{branch_id}` param matches the page's own route param (platform validation
    # enforces endpoint-params ⊆ route-params), and `disabled_when: "archived"` is
    # the literal flag the shell keys on.
    composer = thread_page["composer"]
    assert composer["endpoint"] == "/ui-api/pages/branches/{branch_id}/messages"
    assert composer["placeholder"] == "Reply in this branch…"
    assert composer["disabled_when"] == "archived"


def test_heartbeat_reasserts_governance_manifest_every_beat():
    # The governance wrapper wires into the shared SDK loop (issue #50): the
    # loop keeps re-POSTing (self-healing, issue #39). Uses the imported harness.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "governance", "outcome": "unchanged"})

    got = run_heartbeat_until(
        registration.registration_heartbeat,
        handler,
        beats=3,
        platform_url="http://platform.example",
        interval=0.01,
    )
    assert got >= 3


def test_manifest_is_accepted_by_the_platform_model():
    """The manifest governance posts validates against the platform's manifest
    model — the contract both sides share (the platform accepts the surfaces
    mapping). This is the only place governance touches a platform symbol, and
    it's a TEST-only check of contract compatibility, not a runtime import."""
    from snowline_platform.manifest import PluginManifest

    m = PluginManifest(**registration.build_manifest("http://gov.example:8801"))
    assert m.name == "governance"
    assert m.surfaces == {"/mcp": "main", "/shadow/mcp": "shadow"}
    # The `ui` block validates against the platform's own UIBlock/UIWidget/
    # UIPage models too (issue #55) — the same contract both sides share.
    assert m.ui is not None
    assert m.ui.contract_version == 1
    assert [w.id for w in m.ui.widgets] == [
        "shadow-activity",
        "unreconciled-decisions",
    ]
    assert [p.id for p in m.ui.pages] == ["shadow-branches", "shadow-branch"]
    assert [p.route for p in m.ui.pages] == ["/shadow", "/shadow/{branch_id}"]
    # The thread page's composer validates against the platform's UIComposer /
    # UIPage._valid_composer_for_kind (endpoint-params ⊆ route-params, PR #72).
    thread_page = m.ui.pages[1]
    assert thread_page.composer is not None
    assert (
        thread_page.composer.endpoint == "/ui-api/pages/branches/{branch_id}/messages"
    )
    assert thread_page.composer.disabled_when == "archived"
