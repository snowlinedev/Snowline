"""Keep FastMCP from JSON-decoding free-text tool arguments.

FastMCP's `FuncMetadata.pre_parse_json` runs `json.loads` on every string
argument whose annotation is not exactly `str`, to rescue clients that send a
list or object as a JSON-encoded string. For governance that is right for the
container params (`governs: str | list[str] | None`, `code_refs`) and wrong for
every free-text param typed `str | None` — an artifact `body_snapshot`, a
`summary`, a decision `rationale`. A JSON document body (a store listing, a
policy set) arrived as a dict and failed validation; a rationale reading
`null` silently became None (snowlinedev/Snowline#315).

`install_text_safe_pre_parse(mcp)` swaps each registered tool's metadata for a
subclass whose pre-parse only touches params whose annotation can actually hold
a list, dict, tuple, set or model. Text params reach the tool exactly as sent,
including an explicit `null`. Call it once, after every tool is registered.
"""

from __future__ import annotations

import json
import types
import typing
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from pydantic import BaseModel

_CONTAINERS = (list, dict, tuple, set, frozenset)


def admits_container(annotation: Any) -> bool:
    """True if a value of `annotation` may be a list/dict/tuple/set or a model —
    the only shapes a JSON-encoded string argument should be decoded into."""
    if annotation is Any:
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return admits_container(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return any(admits_container(a) for a in typing.get_args(annotation))
    if origin is not None:
        return isinstance(origin, type) and issubclass(origin, _CONTAINERS)
    if isinstance(annotation, type):
        return issubclass(annotation, (*_CONTAINERS, BaseModel))
    return False


class TextSafeFuncMetadata(FuncMetadata):
    """`FuncMetadata` whose JSON pre-parse skips params that cannot hold a
    container. Otherwise identical to FastMCP's: a string that decodes to a
    bare scalar is still left alone."""

    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        new_data = data.copy()
        fields = {}
        for name, info in self.arg_model.model_fields.items():
            fields[name] = info
            if info.alias:
                fields[info.alias] = info
        for key, value in data.items():
            info = fields.get(key)
            if info is None or not isinstance(value, str):
                continue
            if not admits_container(info.annotation):
                continue
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, str | int | float):
                continue
            new_data[key] = parsed
        return new_data


def install_text_safe_pre_parse(mcp: FastMCP) -> None:
    """Swap every registered tool's metadata for `TextSafeFuncMetadata`."""
    for tool in mcp._tool_manager.list_tools():
        meta = tool.fn_metadata
        if isinstance(meta, TextSafeFuncMetadata):
            continue
        tool.fn_metadata = TextSafeFuncMetadata.model_construct(
            **{name: getattr(meta, name) for name in type(meta).model_fields}
        )
