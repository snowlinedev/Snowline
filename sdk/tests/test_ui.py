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
    assert ui.PAGE_KINDS == frozenset({"table", "thread", "document", "board"})
    assert ui.PAGE_KIND_TABLE == "table"
    assert ui.PAGE_KIND_THREAD == "thread"
    assert ui.PAGE_KIND_DOCUMENT == "document"
    assert ui.PAGE_KIND_BOARD == "board"


def test_ui_kinds_is_the_union():
    assert ui.UI_KINDS == ui.WIDGET_KINDS | ui.PAGE_KINDS
    assert ui.UI_KINDS == frozenset(
        {"stat", "list", "table", "thread", "document", "board"}
    )


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


def test_board_drawer_shape_is_documented():
    # §4.2a "The drawer": the board payload's optional dockable side list for
    # work that sits OUTSIDE the hierarchy (snowlinedev/snowline-pm decision
    # b9116311). OPTIONAL and additive — a board payload without it renders as
    # it always did — so the shape doc must say "optional" and must not grow a
    # second required field beside `nodes`. Documentation, not an SDK-side
    # schema: the shell's board validator is the enforcement surface (it fails
    # the whole board visible on a malformed drawer, §4.4).
    assert set(ui.DRAWER_SHAPE) == {"title", "nodes", "empty", "count_label"}
    assert ui.DRAWER_SHAPE["title"].startswith("required")
    assert ui.DRAWER_SHAPE["nodes"].startswith("required")
    assert ui.DRAWER_SHAPE["empty"].startswith("optional")
    assert ui.DRAWER_SHAPE["count_label"].startswith("optional")
    # Reachable from the kind a plugin author actually looks up, and optional
    # there too.
    assert "drawer" in ui.BOARD_SHAPE
    assert ui.BOARD_SHAPE["drawer"].startswith("optional")
    assert ui.UI_KIND_SHAPES[ui.PAGE_KIND_BOARD] is ui.BOARD_SHAPE
    # The drawer is a BOARD field only — no other kind grew one.
    assert [k for k, v in ui.UI_KIND_SHAPES.items() if "drawer" in v] == [
        ui.PAGE_KIND_BOARD
    ]


def test_package_reexports_ui_constants():
    import snowline_plugin_sdk as sdk

    assert sdk.UI_CONTRACT_VERSION == ui.UI_CONTRACT_VERSION
    assert sdk.UI_KINDS == ui.UI_KINDS
    assert sdk.UI_KIND_SHAPES is ui.UI_KIND_SHAPES
    # Every individual PAGE_KIND_* constant is re-exported at the top level
    # too (a plugin author reasonably expects `from snowline_plugin_sdk import
    # PAGE_KIND_X` to work the same way for every kind, not just some — a
    # newly-added kind missing here is an inconsistent, undocumented import
    # surface, not a fail-visible-at-render concern like the kind vocabulary
    # itself).
    assert sdk.PAGE_KIND_TABLE == ui.PAGE_KIND_TABLE
    assert sdk.PAGE_KIND_THREAD == ui.PAGE_KIND_THREAD
    assert sdk.PAGE_KIND_DOCUMENT == ui.PAGE_KIND_DOCUMENT
    assert sdk.PAGE_KIND_BOARD == ui.PAGE_KIND_BOARD
