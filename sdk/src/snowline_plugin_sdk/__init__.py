"""Snowline plugin SDK — the published, versioned contract a plugin pins to
consume the platform's governance core (issue #19).

Three halves were specified in the frozen monolith / built since:
  * REQUEST/RESPONSE — a typed async MCP client (`SnowlineCore`) over the core's
    MCP tool surface. This is DEFERRED here as a follow-up: the platform's
    surfaces differ from the monolith's `/core`, and the typed client pulls the
    heavyweight `mcp` dependency. It is intentionally NOT shipped in this package
    yet (see #19 / the package pyproject note).
  * EVENT — `verify_event`, which HMAC-verifies + parses governance's signed
    decision webhooks, version-checked against the vendored `CONTRACT_VERSION`.
    This half is shipped here and is the contract the drift-guard test pins.
  * UI — `ui.py`'s vendored `UI_CONTRACT_VERSION` + kind vocabulary + shape docs
    (ui-shell.md §3/§4), for a plugin registering a manifest `ui` block or
    implementing its `/ui-api` responses. Also pinned by a drift-guard test.

Built since: REPLICATION (`snowline_plugin_sdk.replication`, issue #77 /
replication-continuity §3) — the emit/ingest envelope mechanics, the §3.1 retry
class, and the §5 replication-admin surface a plugin adopts to replicate its
store. Rides the `[replication]` extra (sqlalchemy/fastapi) and is imported
EXPLICITLY, like `.registration` on `[client]` — this package root stays
stdlib-only.
"""

from .contract import (
    CONTRACT_VERSION,
    EVENT_DECISION_MARKED_COMPATIBLE,
    EVENT_DECISION_RECORDED,
    EVENT_DECISION_SUPERSEDED,
    EVENT_TYPES,
    IncompatibleContractVersion,
    check_contract_version,
)
from .events import BadSignature, verify_event
from .ui import (
    PAGE_KIND_BOARD,
    PAGE_KIND_DOCUMENT,
    PAGE_KIND_TABLE,
    PAGE_KIND_THREAD,
    PAGE_KINDS,
    UI_CONTRACT_VERSION,
    UI_KIND_SHAPES,
    UI_KINDS,
    WIDGET_KIND_LIST,
    WIDGET_KIND_STAT,
    WIDGET_KINDS,
)

__all__ = [
    "verify_event",
    "BadSignature",
    "IncompatibleContractVersion",
    "check_contract_version",
    "CONTRACT_VERSION",
    "EVENT_DECISION_RECORDED",
    "EVENT_DECISION_SUPERSEDED",
    "EVENT_DECISION_MARKED_COMPATIBLE",
    "EVENT_TYPES",
    "UI_CONTRACT_VERSION",
    "WIDGET_KIND_STAT",
    "WIDGET_KIND_LIST",
    "WIDGET_KINDS",
    "PAGE_KIND_TABLE",
    "PAGE_KIND_THREAD",
    "PAGE_KIND_DOCUMENT",
    "PAGE_KIND_BOARD",
    "PAGE_KINDS",
    "UI_KINDS",
    "UI_KIND_SHAPES",
]
