"""The manifest `ui` block (ui-shell.md §3): validation at registration time.

Structural shape errors (duplicate ids, a `data` path not under `/ui-api/`, a
malformed route, an unknown top-level `ui` field) reject the WHOLE manifest —
the same fail-loud posture as `SNOWLINE_SURFACE_PLUGINS`. An unknown `kind` or
a future `contract_version`, by contrast, register fine — those fail visible
at render (§4.4), not at registration (see `test_plugins_routes.py` for the
route-level (`POST /plugins` -> 422) equivalents of the reject cases)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from snowline_platform.manifest import PluginManifest


def _widget(**overrides) -> dict:
    return {
        "id": "w1",
        "slot": "home",
        "kind": "stat",
        "data": "/ui-api/widgets/w1",
    } | overrides


def _page(**overrides) -> dict:
    return {
        "id": "p1",
        "route": "/p1",
        "kind": "table",
        "data": "/ui-api/pages/p1",
    } | overrides


def _manifest(ui: dict | None) -> PluginManifest:
    return PluginManifest(name="gov", base_url="http://x", ui=ui)


def test_ui_block_absent_stays_fine():
    m = _manifest(None)
    assert m.ui is None


def test_valid_ui_block_registers():
    m = _manifest(
        {
            "contract_version": 1,
            "widgets": [_widget()],
            "pages": [_page(), _page(id="p2", route="/p1/{id}", nav=False)],
        }
    )
    assert m.ui.contract_version == 1
    assert [w.id for w in m.ui.widgets] == ["w1"]
    assert [p.id for p in m.ui.pages] == ["p1", "p2"]


def test_duplicate_widget_id_rejected():
    with pytest.raises(ValidationError, match="duplicate widget id"):
        _manifest({"widgets": [_widget(), _widget()]})


def test_duplicate_page_id_rejected():
    with pytest.raises(ValidationError, match="duplicate page id"):
        _manifest({"pages": [_page(), _page(route="/other")]})


@pytest.mark.parametrize(
    "bad_data",
    ["/other/path", "ui-api/widgets/w1", "/UI-API/widgets/w1", ""],
)
def test_widget_data_must_start_with_ui_api(bad_data):
    with pytest.raises(ValidationError, match="must start with '/ui-api/'"):
        _manifest({"widgets": [_widget(data=bad_data)]})


def test_page_data_must_start_with_ui_api():
    with pytest.raises(ValidationError, match="must start with '/ui-api/'"):
        _manifest({"pages": [_page(data="/mcp/pages/p1")]})


@pytest.mark.parametrize(
    "bad_route",
    [
        "p1",  # missing leading '/'
        "/p1/",  # trailing slash
        "/p1//p2",  # empty segment
        "/p1/{}",  # empty param name
        "/p1/{branch",  # unclosed brace
        "/p1/branch}",  # stray brace
        "/p1/{1branch}",  # param name must be a valid identifier
    ],
)
def test_malformed_route_rejected(bad_route):
    with pytest.raises(ValidationError):
        _manifest({"pages": [_page(route=bad_route)]})


def test_root_route_is_valid():
    m = _manifest({"pages": [_page(route="/")]})
    assert m.ui.pages[0].route == "/"


def test_route_path_param_templates_are_valid():
    m = _manifest({"pages": [_page(route="/branches/{branch}/nodes/{node_id}")]})
    assert m.ui.pages[0].route == "/branches/{branch}/nodes/{node_id}"


def test_unknown_top_level_ui_field_rejected():
    with pytest.raises(ValidationError, match="bogus"):
        _manifest({"widgets": [], "bogus": True})


def test_unknown_widget_slot_rejected():
    with pytest.raises(ValidationError):
        _manifest({"widgets": [_widget(slot="sidebar")]})


def test_unknown_kind_registers_ok():
    # Kinds are shell-version-dependent (§4.4 fail-visible) — an unrecognized
    # one is NOT a registration-time concern.
    m = _manifest({"widgets": [_widget(kind="a-kind-from-the-future")]})
    assert m.ui.widgets[0].kind == "a-kind-from-the-future"


def test_future_contract_version_registers_ok():
    m = _manifest({"contract_version": 999, "widgets": [_widget()]})
    assert m.ui.contract_version == 999
