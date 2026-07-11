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

from snowline_platform import manifest
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


def _composer(**overrides) -> dict:
    return {"endpoint": "/ui-api/pages/p1/messages"} | overrides


def _thread_page(**overrides) -> dict:
    return {
        "id": "thread1",
        "route": "/branches/{branch}",
        "kind": "thread",
        "data": "/ui-api/pages/branches/{branch}",
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


# --- composer (shadow-conversations.md §4) ----------------------------------


def test_valid_composer_on_thread_page_registers():
    m = _manifest(
        {
            "pages": [
                _thread_page(
                    composer={
                        "endpoint": "/ui-api/pages/branches/{branch}/messages",
                        "placeholder": "Reply in this branch…",
                        "disabled_when": "archived",
                    }
                )
            ]
        }
    )
    composer = m.ui.pages[0].composer
    assert composer.endpoint == "/ui-api/pages/branches/{branch}/messages"
    assert composer.placeholder == "Reply in this branch…"
    assert composer.disabled_when == "archived"


def test_composer_optional_fields_default_none():
    m = _manifest(
        {
            "pages": [
                _thread_page(
                    composer={"endpoint": "/ui-api/pages/branches/{branch}/messages"}
                )
            ]
        }
    )
    composer = m.ui.pages[0].composer
    assert composer.placeholder is None
    assert composer.disabled_when is None


def test_composer_with_no_route_params_is_fine():
    m = _manifest(
        {
            "pages": [
                _page(
                    id="p1",
                    route="/p1",
                    kind="thread",
                    data="/ui-api/pages/p1",
                    composer=_composer(),
                )
            ]
        }
    )
    assert m.ui.pages[0].composer.endpoint == "/ui-api/pages/p1/messages"


def test_composer_on_non_thread_page_rejected():
    with pytest.raises(ValidationError, match="only valid on 'thread' pages"):
        _manifest({"pages": [_page(kind="table", composer=_composer())]})


def test_composer_endpoint_must_start_with_ui_api():
    with pytest.raises(ValidationError, match="must start with '/ui-api/'"):
        _manifest(
            {"pages": [_thread_page(composer=_composer(endpoint="/mcp/messages"))]}
        )


@pytest.mark.parametrize(
    "bad_endpoint",
    [
        "/ui-api/pages/branches/{branch}/",  # trailing slash
        "/ui-api/pages//messages",  # empty segment
        "/ui-api/pages/{}/messages",  # empty param name
        "/ui-api/pages/{branch/messages",  # unclosed brace
        "/ui-api/pages/branch}/messages",  # stray brace
        "/ui-api/pages/{1branch}/messages",  # invalid identifier
    ],
)
def test_composer_endpoint_malformed_segment_rejected(bad_endpoint):
    with pytest.raises(ValidationError):
        _manifest(
            {"pages": [_thread_page(composer=_composer(endpoint=bad_endpoint))]}
        )


def test_composer_unknown_field_rejected():
    with pytest.raises(ValidationError):
        _manifest(
            {
                "pages": [
                    _thread_page(
                        composer={
                            "endpoint": "/ui-api/pages/branches/{branch}/messages",
                            "bogus": True,
                        }
                    )
                ]
            }
        )


def test_composer_typo_on_page_rejected():
    # `extra="forbid"` on UIPage: a misspelled `composer` key must 422 at
    # registration, not silently drop and leave the write seam dead (every
    # POST 403ing with nothing to explain why).
    page = _thread_page()
    page["composeer"] = {"endpoint": "/ui-api/pages/branches/{branch}/messages"}
    with pytest.raises(ValidationError):
        _manifest({"pages": [page]})


def test_composer_endpoint_dot_segment_rejected():
    # '.'/'..' are valid literal tokens in a route slug, but the proxy
    # dot-collapses request paths BEFORE matching, so a declared endpoint
    # containing one could never be reached — fail loud, not dead-on-arrival.
    with pytest.raises(ValidationError, match="dot-segment|'\\.'"):
        _manifest(
            {
                "pages": [
                    _thread_page(
                        composer=_composer(
                            endpoint="/ui-api/pages/branches/{branch}/../messages"
                        )
                    )
                ]
            }
        )


def test_composer_endpoint_param_not_in_route_rejected():
    with pytest.raises(ValidationError, match="not present in route"):
        _manifest(
            {
                "pages": [
                    _thread_page(
                        composer=_composer(
                            endpoint="/ui-api/pages/branches/{other}/messages"
                        )
                    )
                ]
            }
        )


def test_composer_endpoint_may_use_fewer_params_than_route():
    # The route can carry MORE params than the endpoint uses — only params
    # the endpoint references must be present in the route, not the reverse.
    m = _manifest(
        {
            "pages": [
                _page(
                    id="p1",
                    route="/branches/{branch}/nodes/{node_id}",
                    kind="thread",
                    data="/ui-api/pages/branches/{branch}/nodes/{node_id}",
                    composer=_composer(
                        endpoint="/ui-api/pages/branches/{branch}/messages"
                    ),
                )
            ]
        }
    )
    assert m.ui.pages[0].composer.endpoint == "/ui-api/pages/branches/{branch}/messages"


# --- page actions[] (ui-shell.md §5, issue #123) ----------------------------


def _action(**overrides) -> dict:
    return {
        "id": "new-branch",
        "label": "New branch",
        "endpoint": "/ui-api/pages/branches",
        "fields": [
            {"name": "scope", "kind": "text", "required": True},
            {"name": "opening_message", "kind": "multiline"},
        ],
    } | overrides


def test_valid_actions_on_any_page_kind_register():
    # actions are valid on a `table` page (unlike the thread-only composer) —
    # the "New branch" affordance lives on the branches TABLE page.
    m = _manifest({"pages": [_page(actions=[_action()])]})
    action = m.ui.pages[0].actions[0]
    assert action.id == "new-branch"
    assert action.label == "New branch"
    assert action.endpoint == "/ui-api/pages/branches"
    assert [f.name for f in action.fields] == ["scope", "opening_message"]
    # field defaults round-trip.
    assert action.fields[0].kind == "text" and action.fields[0].required is True
    assert action.fields[1].kind == "multiline" and action.fields[1].required is False


def test_actions_absent_defaults_empty():
    m = _manifest({"pages": [_page()]})
    assert m.ui.pages[0].actions == []


def test_action_field_scope_kind_round_trips():
    # The `scope` field kind (ui-shell.md §5.1: a text input with a <datalist>
    # typeahead over the platform's scope slugs) round-trips like any other —
    # `kind` is a free string, so this is just a documented value, not a new
    # validation branch. It's in the DOCUMENTED vocabulary the drift test pins.
    m = _manifest(
        {"pages": [_page(actions=[_action(fields=[{"name": "scope", "kind": "scope"}])])]}
    )
    field = m.ui.pages[0].actions[0].fields[0]
    assert field.name == "scope" and field.kind == "scope"
    assert "scope" in manifest.ACTION_FIELD_KINDS


def test_action_field_label_defaults_none():
    m = _manifest({"pages": [_page(actions=[_action(fields=[{"name": "scope"}])])]})
    field = m.ui.pages[0].actions[0].fields[0]
    assert field.label is None and field.kind == "text"


def test_action_endpoint_must_start_with_ui_api():
    with pytest.raises(ValidationError):
        _manifest({"pages": [_page(actions=[_action(endpoint="/mcp/create")])]})


def test_action_endpoint_param_not_in_route_rejected():
    # An endpoint templating a param the page route can't supply is a dead
    # write seam — 422 at registration, not a 403 forever at request time.
    with pytest.raises(ValidationError):
        _manifest(
            {
                "pages": [
                    _page(
                        route="/shadow",
                        actions=[_action(endpoint="/ui-api/pages/branches/{branch}")],
                    )
                ]
            }
        )


def test_action_endpoint_param_present_in_route_is_fine():
    m = _manifest(
        {
            "pages": [
                _page(
                    id="p1",
                    route="/shadow/{branch}",
                    data="/ui-api/pages/branches/{branch}",
                    actions=[_action(endpoint="/ui-api/pages/branches/{branch}/act")],
                )
            ]
        }
    )
    assert m.ui.pages[0].actions[0].endpoint == "/ui-api/pages/branches/{branch}/act"


def test_duplicate_action_id_rejected():
    with pytest.raises(ValidationError):
        _manifest({"pages": [_page(actions=[_action(), _action(label="Dup")])]})


def test_duplicate_action_field_name_rejected():
    with pytest.raises(ValidationError):
        _manifest(
            {
                "pages": [
                    _page(
                        actions=[
                            _action(fields=[{"name": "scope"}, {"name": "scope"}])
                        ]
                    )
                ]
            }
        )


def test_unknown_action_field_rejected():
    with pytest.raises(ValidationError):
        _manifest(
            {
                "pages": [
                    _page(actions=[_action(fields=[{"name": "scope", "nope": 1}])])
                ]
            }
        )


def test_unknown_action_key_rejected():
    with pytest.raises(ValidationError):
        _manifest({"pages": [_page(actions=[_action(method="POST")])]})


def test_unknown_action_field_kind_registers_ok():
    # `kind` is a FREE string (fail-visible at render), like widget/page kinds —
    # an unrecognized value must NOT reject the manifest.
    m = _manifest(
        {"pages": [_page(actions=[_action(fields=[{"name": "x", "kind": "wat"}])])]}
    )
    assert m.ui.pages[0].actions[0].fields[0].kind == "wat"
