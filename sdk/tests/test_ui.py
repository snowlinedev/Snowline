"""SDK-own unit tests for the vendored UI contract constants (ui-shell.md
§3/§4). The producer↔consumer equality check against the platform's copy
lives in the platform repo (tests/test_ui_contract_drift.py, a dev-only
import of this SDK) — these are just this package's own sanity checks."""

from __future__ import annotations

from snowline_plugin_sdk import ui


def test_ui_contract_version():
    assert ui.UI_CONTRACT_VERSION == 1


def test_widget_kinds():
    assert ui.WIDGET_KINDS == frozenset({"stat", "list"})
    assert ui.WIDGET_KIND_STAT == "stat"
    assert ui.WIDGET_KIND_LIST == "list"


def test_page_kinds():
    assert ui.PAGE_KINDS == frozenset({"table", "thread", "document"})
    assert ui.PAGE_KIND_TABLE == "table"
    assert ui.PAGE_KIND_THREAD == "thread"
    assert ui.PAGE_KIND_DOCUMENT == "document"


def test_ui_kinds_is_the_union():
    assert ui.UI_KINDS == ui.WIDGET_KINDS | ui.PAGE_KINDS
    assert ui.UI_KINDS == frozenset({"stat", "list", "table", "thread", "document"})


def test_every_kind_has_a_shape_doc():
    assert set(ui.UI_KIND_SHAPES) == ui.UI_KINDS


def test_action_shape_is_specified():
    # §5 / issue #123: page actions[] moved from reserved to specified. The
    # shape docs cover the same field vocabulary the platform's UIAction /
    # UIActionField models enforce (pinned equal by the platform's
    # test_ui_contract_drift.py). Still documentation, not an SDK-side schema —
    # the platform is the enforcement surface.
    assert set(ui.ACTION_SHAPE) == ui.ACTION_FIELDS == {"id", "label", "endpoint", "fields"}
    assert set(ui.ACTION_FIELD_SHAPE) == ui.ACTION_FIELD_FIELDS == {
        "name",
        "label",
        "kind",
        "required",
    }
    assert ui.ACTION_FIELD_KINDS == {"text", "multiline", "scope"}
    # The action endpoint's response contract (the generic success-navigation
    # href the shell follows).
    assert set(ui.ACTION_RESPONSE_SHAPE) == {"navigate"}


def test_package_reexports_ui_constants():
    import snowline_plugin_sdk as sdk

    assert sdk.UI_CONTRACT_VERSION == ui.UI_CONTRACT_VERSION
    assert sdk.UI_KINDS == ui.UI_KINDS
    assert sdk.UI_KIND_SHAPES is ui.UI_KIND_SHAPES
