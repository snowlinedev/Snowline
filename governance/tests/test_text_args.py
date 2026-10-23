"""Free-text tool arguments reach governance exactly as sent (Snowline#315).

FastMCP JSON-decodes any string argument whose annotation is not exactly
`str`, which turned a JSON document `body_snapshot` into a dict (so JSON
artifacts could be registered over MCP but never revised) and would do the
same to a `summary` or `rationale`. `install_text_safe_pre_parse` limits the
pre-parse to params that can hold a container. These tests sweep every tool on
both surfaces, so a new text param is covered without being listed here.
No DB / no platform needed (stub clients; arguments are validated, tools are
never invoked)."""

from __future__ import annotations

import typing

import anyio
import pytest

from snowline_governance.mcp_surface import build_main_surface, build_shadow_surface
from snowline_governance.text_args import TextSafeFuncMetadata, admits_container


class _NoopScopeClient:
    def resolve(self, slug: str): return None
    def ancestors(self, slug: str): return []


JSON_DOC = '{"schema_version": 1, "app": {"key": "vial"}}'


def _surfaces():
    return {
        "main": build_main_surface(scope_client=_NoopScopeClient()),
        "shadow": build_shadow_surface(scope_client=_NoopScopeClient()),
    }


def _tools():
    for surface_name, mcp in _surfaces().items():
        for tool in mcp._tool_manager.list_tools():
            yield surface_name, tool


def _text_params(tool):
    """(name, annotation) for every param that is text, possibly optional."""
    for name, info in tool.fn_metadata.arg_model.model_fields.items():
        args = set(typing.get_args(info.annotation)) or {info.annotation}
        if str in args and not admits_container(info.annotation):
            yield name, info


def test_every_tool_on_both_surfaces_uses_the_text_safe_metadata():
    for surface_name, tool in _tools():
        assert isinstance(tool.fn_metadata, TextSafeFuncMetadata), (
            surface_name,
            tool.name,
        )


def test_json_text_in_any_text_param_is_not_decoded():
    checked = 0
    for surface_name, tool in _tools():
        for name, _info in _text_params(tool):
            parsed = tool.fn_metadata.pre_parse_json({name: JSON_DOC})
            assert parsed[name] == JSON_DOC, (surface_name, tool.name, name)
            checked += 1
    # body_snapshot, body, summary, rationale, … — the sweep found real params.
    assert checked >= 10


@pytest.mark.parametrize(
    ("tool", "param", "required"),
    [
        ("register_artifact", "body", {}),
        ("revise_artifact", "body_snapshot", {"artifact_id": "a1"}),
        ("revise_artifact", "summary", {"artifact_id": "a1"}),
        ("record_decision", "rationale", {"scope": "s", "decision": "d"}),
    ],
)
def test_json_document_validates_as_the_string_it_was_sent(tool, param, required):
    meta = _surfaces()["main"]._tool_manager.get_tool(tool).fn_metadata
    args = meta.arg_model.model_validate(
        meta.pre_parse_json({**required, param: JSON_DOC})
    )
    assert getattr(args, param) == JSON_DOC


def test_explicit_null_still_means_none_for_optional_text():
    meta = _surfaces()["main"]._tool_manager.get_tool("revise_artifact").fn_metadata
    args = meta.arg_model.model_validate(
        meta.pre_parse_json({"artifact_id": "a1", "body_snapshot": None})
    )
    assert args.body_snapshot is None
    # The literal text "null" is text, not a JSON null.
    args = meta.arg_model.model_validate(
        meta.pre_parse_json({"artifact_id": "a1", "summary": "null"})
    )
    assert args.summary == "null"


def test_container_params_still_accept_a_json_encoded_string():
    """The pre-parse FastMCP added for clients that send a list as a string
    still works where a list is legal."""
    meta = _surfaces()["main"]._tool_manager.get_tool("set_governs").fn_metadata
    parsed = meta.pre_parse_json({"governs": '["acme/a", "acme/b"]'})
    assert parsed["governs"] == ["acme/a", "acme/b"]


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (str, False),
        (str | None, False),
        (int | None, False),
        (list[str], True),
        (str | list[str] | None, True),
        (dict, True),
        (typing.Any, True),
        (typing.Annotated[list[dict], "x"], True),
    ],
)
def test_admits_container(annotation, expected):
    assert admits_container(annotation) is expected


def test_install_is_idempotent_and_keeps_schemas():
    mcp = build_main_surface(scope_client=_NoopScopeClient())
    before = {t.name: t.inputSchema for t in anyio.run(mcp.list_tools)}
    from snowline_governance.text_args import install_text_safe_pre_parse

    install_text_safe_pre_parse(mcp)
    after = {t.name: t.inputSchema for t in anyio.run(mcp.list_tools)}
    assert before == after
