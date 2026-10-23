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
subclass that hands FastMCP's own pre-parse only the arguments whose param can
hold something other than plain text. Text params reach the tool exactly as
sent, including an explicit `null`. Call it once, after every tool is
registered.
"""

from __future__ import annotations

import enum
import types
import typing
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from pydantic import PrivateAttr

# Annotations that can only ever be a scalar. Anything not provably made of
# these keeps FastMCP's JSON decode (the safe default: an unresolved forward
# ref, a dataclass or a NewType behaves exactly as it did before this module).
_SCALARS = (str, int, float, bool, type(None))


def is_text_only(annotation: Any) -> bool:
    """True if every value `annotation` admits is a scalar (text, number,
    bool, None, a Literal or an Enum) — so a JSON string sent for it must
    never be decoded into a container."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return is_text_only(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return all(is_text_only(a) for a in typing.get_args(annotation))
    if origin is typing.Literal:
        return True
    if annotation is None:
        return True
    if isinstance(annotation, type):
        return issubclass(annotation, (*_SCALARS, enum.Enum))
    return False


class TextSafeFuncMetadata(FuncMetadata):
    """`FuncMetadata` whose JSON pre-parse never touches a text-only param.
    Everything else goes through FastMCP's own `pre_parse_json` unchanged."""

    _text_keys: frozenset[str] = PrivateAttr(default=frozenset())

    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        decodable = {k: v for k, v in data.items() if k not in self._text_keys}
        return {**data, **super().pre_parse_json(decodable)}


def _text_keys(meta: FuncMetadata) -> frozenset[str]:
    keys: set[str] = set()
    for name, info in meta.arg_model.model_fields.items():
        if is_text_only(info.annotation):
            keys.add(name)
            if info.alias:
                keys.add(info.alias)
    return frozenset(keys)


def install_text_safe_pre_parse(mcp: FastMCP) -> None:
    """Swap every registered tool's metadata for `TextSafeFuncMetadata`."""
    for tool in mcp._tool_manager.list_tools():
        meta = tool.fn_metadata
        if isinstance(meta, TextSafeFuncMetadata):
            continue
        safe = TextSafeFuncMetadata.model_construct(
            **{name: getattr(meta, name) for name in type(meta).model_fields}
        )
        safe._text_keys = _text_keys(meta)
        tool.fn_metadata = safe
