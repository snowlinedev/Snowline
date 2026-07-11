"""UI contract constants + kind-shape documentation (ui-shell.md §3/§4).

These are a deliberate COPY of the platform's source of truth
(`snowline_platform.manifest`: `UI_CONTRACT_VERSION`, `UI_WIDGET_KINDS`,
`UI_PAGE_KINDS`, `UI_KINDS`). The SDK is the published, light dependency
EXTERNAL plugins install (contract.py's docstring explains why it stays
stdlib-only — no pydantic, no platform import at runtime): a plugin author
building a `ui` manifest block or a `/ui-api` response reads these constants
+ shape docs instead of reverse-engineering the shell's renderer. A
drift-guard test (`tests/test_ui_contract_drift.py` in the platform repo, a
dev-only import of this SDK — mirroring `governance/tests/test_contract_drift.py`)
pins this copy EQUAL to the platform's so the two can never silently fork.

Unlike `contract.py`'s `CONTRACT_VERSION` (which gates a hard
accept/reject check), `UI_CONTRACT_VERSION` here is documentation only: per
spec §3, an unfamiliar `contract_version` on a manifest's `ui` block — and an
unfamiliar `kind` — REGISTER fine and fail visible at render (§4.4), so this
module intentionally ships no `check_ui_contract_version`-style gate. Kind
shapes below are illustrative dicts/docstrings, not schemas — nothing here
validates a plugin's actual `/ui-api` response at runtime; that is the shell's
job, and a malformed one renders the §4.4 error card.

Pure constants — no imports beyond stdlib.
"""

# Guards the manifest `ui` block's SHAPE (ui-shell.md §3), not the kind
# vocabulary below — kept in lockstep with
# `snowline_platform.manifest.UI_CONTRACT_VERSION` by the drift-guard test.
UI_CONTRACT_VERSION: int = 1

# --- Widget kinds (home grid, §4.1) -----------------------------------------

WIDGET_KIND_STAT: str = "stat"
WIDGET_KIND_LIST: str = "list"

WIDGET_KINDS: frozenset[str] = frozenset({WIDGET_KIND_STAT, WIDGET_KIND_LIST})

# --- Page kinds (§4.2) -------------------------------------------------------

PAGE_KIND_TABLE: str = "table"
PAGE_KIND_THREAD: str = "thread"
PAGE_KIND_DOCUMENT: str = "document"

PAGE_KINDS: frozenset[str] = frozenset(
    {PAGE_KIND_TABLE, PAGE_KIND_THREAD, PAGE_KIND_DOCUMENT}
)

# The full v1 kind vocabulary — what the drift-guard test pins against the
# platform's `UI_KINDS`. `search` (§4.2) is anticipated but deferred and
# deliberately absent from both copies until it ships.
UI_KINDS: frozenset[str] = WIDGET_KINDS | PAGE_KINDS

# --- Composer (thread pages' write seam, shadow-conversations.md §4) -------
#
# The field vocabulary of a `thread` page's optional `composer` object —
# mirrors `UI_KINDS`' drift-guard treatment: the platform
# (`snowline_platform.manifest.COMPOSER_FIELDS`) is the source of truth,
# pinned equal to this copy by `test_ui_contract_drift.py`.
COMPOSER_FIELDS: frozenset[str] = frozenset({"endpoint", "placeholder", "disabled_when"})

# Page `actions[]` field vocabulary (ui-shell.md §5, issue #123) — mirrors
# `COMPOSER_FIELDS`' drift-guard treatment: the platform
# (`snowline_platform.manifest`: `ACTION_FIELDS`/`ACTION_FIELD_FIELDS`/
# `ACTION_FIELD_KINDS`, pinned to the real `UIAction`/`UIActionField` models) is
# the source of truth, and `test_ui_contract_drift.py` pins these equal.
ACTION_FIELDS: frozenset[str] = frozenset({"id", "label", "endpoint", "fields"})
ACTION_FIELD_FIELDS: frozenset[str] = frozenset({"name", "label", "kind", "required"})
ACTION_FIELD_KINDS: frozenset[str] = frozenset({"text", "multiline", "scope"})

# The /ui-api proxy's POST body cap (shadow-conversations.md §3): a
# conversation message, not an upload. THE shared value — the platform's
# proxy enforcement (`snowline_platform.ui_api.POST_BODY_LIMIT`) is pinned
# equal by `test_ui_contract_drift.py`, and a plugin's write route should
# reject at the same boundary (import THIS constant, don't hardcode 65536)
# so proxy and plugin can't drift apart on what fits.
UI_WRITE_BODY_LIMIT: int = 64 * 1024

# --- Response-contract shapes, by kind (§4.1/§4.2) --------------------------
#
# Each entry documents the plugin-side JSON response body a widget/page's
# `data` endpoint must return for the shell to render that `kind`. These are
# plain dicts for illustration (key -> a short description of the value,
# `"optional"` markers noted in the description) — NOT a schema, and nothing
# in this module enforces them; a plugin that ships a malformed body gets the
# §4.4 error card, not an SDK-side exception.

STAT_SHAPE: dict[str, str] = {
    "value": "required — the number/short string to render",
    "label": "optional — a caption under the value",
    "delta": "optional — a trend value, e.g. '+3' (shell may color by sign)",
    "intent": "optional — a semantic color hint, e.g. 'good'|'bad'|'neutral'",
}

LIST_ITEM_SHAPE: dict[str, str] = {
    "text": "required — the item's label",
    "href": "optional — a shell route this item links to",
    "meta": "optional — small trailing annotation (e.g. a timestamp)",
    "intent": "optional — semantic color hint, same vocabulary as STAT_SHAPE",
}

LIST_SHAPE: dict[str, str] = {
    "items": f"required — list of {LIST_ITEM_SHAPE!r}",
    "empty": "optional — placeholder text/state when items is []",
}

TABLE_COLUMN_SHAPE: dict[str, str] = {
    "key": "required — the field name read off each row's `cells`",
    "label": "required — the column header",
    "kind": "optional — a rendering hint: 'text'|'chip'|'time'|'actor'",
}

TABLE_ROW_SHAPE: dict[str, str] = {
    "cells": "required — values keyed by each column's `key`",
    "href": "optional — a shell route this row links to",
}

TABLE_SHAPE: dict[str, str] = {
    "columns": f"required — list of {TABLE_COLUMN_SHAPE!r}",
    "rows": f"required — list of {TABLE_ROW_SHAPE!r}",
    "empty": "optional — placeholder text/state when rows is []",
}

THREAD_NODE_SHAPE: dict[str, str] = {
    "author": "required — the node's authoring actor",
    "kind": "required — a node-level kind hint (plugin-defined, e.g. 'comment')",
    "markdown": "required — the node body, rendered as markdown",
    "at": "required — an ISO-8601 timestamp",
    "citations": "optional — list of plugin-defined citation references",
}

THREAD_SHAPE: dict[str, str] = {
    "title": "required",
    "meta": "required — small header metadata block",
    "nodes": f"required — ordered list of {THREAD_NODE_SHAPE!r}",
}
# Not part of the response body — `composer` is a manifest-side page
# declaration (see COMPOSER_SHAPE below), documented alongside THREAD_SHAPE
# because it's specific to the `thread` kind.

DOCUMENT_SHAPE: dict[str, str] = {
    "title": "required",
    "markdown": "required — the document body, rendered as markdown",
    "meta": "optional",
}

# `thread` pages' optional `composer` object (shadow-conversations.md §4): an
# input-shaped POST target rendered as a markdown textarea + send button at
# the thread foot. NOT an §4.3 action (button-shaped, confirm semantics) —
# but both ride the same proxy-POST enablement and endpoint-allowlist posture
# (ui-shell.md §5). Registration-time validation (platform
# `manifest.py`/`UIComposer`): 422 if declared on a non-`thread` page kind,
# if `endpoint` doesn't start with '/ui-api/', if `endpoint` references a
# '{param}' not present in the page's `route`, or on any field not listed
# here.
COMPOSER_SHAPE: dict[str, str] = {
    "endpoint": "required — a /ui-api-relative POST target; may template "
    "'{param}' segments matching the page's route params",
    "placeholder": "optional — composer textarea placeholder text",
    "disabled_when": "optional — a flag name the shell looks for in the "
    "thread response's top-level `flags` list to grey out the composer "
    "(e.g. 'archived')",
}

# Kind name -> its response-contract shape doc, for a plugin author to look
# up by the same string they put in a widget/page's `kind` field.
UI_KIND_SHAPES: dict[str, dict[str, str]] = {
    WIDGET_KIND_STAT: STAT_SHAPE,
    WIDGET_KIND_LIST: LIST_SHAPE,
    PAGE_KIND_TABLE: TABLE_SHAPE,
    PAGE_KIND_THREAD: THREAD_SHAPE,
    PAGE_KIND_DOCUMENT: DOCUMENT_SHAPE,
}

# --- Actions (§5, SPECIFIED — issue #123) ------------------------------------
#
# A page may declare an `actions` list: labelled buttons that open a minimal
# form of declared `fields` and POST their values through the /ui-api proxy to
# `endpoint`. The button/form-shaped sibling of the input-shaped `composer` —
# both ride the same proxy-POST enablement + endpoint-allowlist posture (§5).
# The shell renders them GENERICALLY (no plugin-specific UI code): a plugin
# declares the button label, the write endpoint, and the form fields, and the
# shell handles rendering, submission, and — on a 2xx — following an optional
# plugin-relative `navigate` href in the response.
#
# Registration-time validation (platform `manifest.py`/`UIAction`): 422 if
# `endpoint` doesn't start with '/ui-api/', if it references a '{param}' absent
# from the page's `route`, on any unknown action or field key, or on a
# duplicate action id / field name within the page. `kind` on a field stays a
# FREE string — an unknown value falls back to a text control at render, it
# does not reject the manifest.

ACTION_FIELD_SHAPE: dict[str, str] = {
    "name": "required — the JSON key the shell submits this field's value as",
    "label": "optional — the visible field label (defaults to `name`)",
    "kind": "optional — 'text' (single line, default), 'multiline' (textarea), "
    "or 'scope' (text input with a typeahead over the platform's scope slugs)",
    "required": "optional — the shell blocks submit until this is filled "
    "(default false)",
}

ACTION_SHAPE: dict[str, str] = {
    "id": "required — unique within the page's actions",
    "label": "required — the button text",
    "endpoint": "required — a /ui-api-relative POST target; may template "
    "'{param}' segments matching the page's route params",
    "fields": f"optional — the form the shell renders: list of "
    f"{ACTION_FIELD_SHAPE!r} (empty = a bare button posting an empty body)",
}

# The action endpoint's RESPONSE contract (plugin side): a 2xx may carry an
# optional `navigate` — a plugin-relative shell href the shell lands on after a
# successful submit (re-prefixed with `/<plugin>`, same as a table row `href`).
# Everything else in the body is ignored by the generic shell.
ACTION_RESPONSE_SHAPE: dict[str, str] = {
    "navigate": "optional — a plugin-relative shell route to navigate to on "
    "success (e.g. a newly-created resource's page)",
}

__all__ = [
    "UI_CONTRACT_VERSION",
    "WIDGET_KIND_STAT",
    "WIDGET_KIND_LIST",
    "WIDGET_KINDS",
    "PAGE_KIND_TABLE",
    "PAGE_KIND_THREAD",
    "PAGE_KIND_DOCUMENT",
    "PAGE_KINDS",
    "UI_KINDS",
    "STAT_SHAPE",
    "LIST_ITEM_SHAPE",
    "LIST_SHAPE",
    "TABLE_COLUMN_SHAPE",
    "TABLE_ROW_SHAPE",
    "TABLE_SHAPE",
    "THREAD_NODE_SHAPE",
    "THREAD_SHAPE",
    "DOCUMENT_SHAPE",
    "UI_KIND_SHAPES",
    "ACTION_FIELDS",
    "ACTION_FIELD_FIELDS",
    "ACTION_FIELD_KINDS",
    "ACTION_FIELD_SHAPE",
    "ACTION_SHAPE",
    "ACTION_RESPONSE_SHAPE",
    "COMPOSER_FIELDS",
    "COMPOSER_SHAPE",
    "UI_WRITE_BODY_LIMIT",
]
