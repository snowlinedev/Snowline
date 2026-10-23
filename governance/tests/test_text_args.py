"""Free-text tool arguments reach governance exactly as sent (Snowline#315).

FastMCP JSON-decodes any string argument whose annotation is not exactly
`str`, which turned a JSON document `body_snapshot` into a dict (so JSON
artifacts could be registered over MCP but never revised) and would do the
same to a `summary` or `rationale`. `install_text_safe_pre_parse` keeps that
decode away from text-only params. These tests sweep every tool on both
surfaces, so a new text param is covered without being listed here, and one
test goes through FastMCP's real `call_tool` path. No DB / no platform needed.
"""

from __future__ import annotations

import contextlib
import enum
import typing

import anyio
import pytest

from snowline_governance import artifacts, mcp_surface
from snowline_governance.mcp_surface import build_main_surface, build_shadow_surface
from snowline_governance.text_args import TextSafeFuncMetadata, is_text_only


class _NoopScopeClient:
    def resolve(self, slug: str): return None
    def ancestors(self, slug: str): return []


class _NoopMilestoneClient:
    pass


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


def _optional_text_params(tool):
    """Params typed text-but-not-exactly-`str` — the ones FastMCP decoded."""
    for name, info in tool.fn_metadata.arg_model.model_fields.items():
        if info.annotation is not str and is_text_only(info.annotation):
            if str in typing.get_args(info.annotation):
                yield name


def test_every_tool_on_both_surfaces_uses_the_text_safe_metadata():
    for surface_name, tool in _tools():
        assert isinstance(tool.fn_metadata, TextSafeFuncMetadata), (
            surface_name,
            tool.name,
        )


def test_json_text_in_any_optional_text_param_is_not_decoded():
    checked = 0
    for surface_name, tool in _tools():
        for name in _optional_text_params(tool):
            parsed = tool.fn_metadata.pre_parse_json({name: JSON_DOC})
            assert parsed[name] == JSON_DOC, (surface_name, tool.name, name)
            checked += 1
    # body_snapshot, summary, rationale, … — the sweep found real params.
    assert checked >= 10


@pytest.mark.parametrize(
    ("tool", "param", "required"),
    [
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


def test_revise_artifact_receives_a_json_body_verbatim_through_call_tool(monkeypatch):
    """End to end through FastMCP's own tool-call path (Tool.run →
    call_fn_with_arg_validation), with only the DB seam stubbed."""
    seen: dict = {}

    @contextlib.contextmanager
    def _no_db():
        yield object()

    def _capture(session, artifact_id, **kwargs):
        seen.update(kwargs, artifact_id=artifact_id)
        return {"id": artifact_id}

    monkeypatch.setattr(mcp_surface, "session_scope", _no_db)
    monkeypatch.setattr(artifacts, "revise_artifact", _capture)
    mcp = build_main_surface(
        scope_client=_NoopScopeClient(), milestone_client=_NoopMilestoneClient()
    )
    anyio.run(
        mcp.call_tool,
        "revise_artifact",
        {"artifact_id": "a1", "body_snapshot": JSON_DOC, "summary": "null"},
    )
    assert seen["body_snapshot"] == JSON_DOC
    assert seen["summary"] == "null"


def test_explicit_null_still_means_none_for_optional_text():
    meta = _surfaces()["main"]._tool_manager.get_tool("revise_artifact").fn_metadata
    args = meta.arg_model.model_validate(
        meta.pre_parse_json({"artifact_id": "a1", "body_snapshot": None})
    )
    assert args.body_snapshot is None


def test_container_params_still_accept_a_json_encoded_string():
    """The pre-parse FastMCP added for clients that send a list as a string
    still works where a list is legal."""
    meta = _surfaces()["main"]._tool_manager.get_tool("set_governs").fn_metadata
    parsed = meta.pre_parse_json({"governs": '["acme/a", "acme/b"]'})
    assert parsed["governs"] == ["acme/a", "acme/b"]


class _Color(enum.Enum):
    RED = "red"


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (str, True),
        (str | None, True),
        (int | None, True),
        (bool, True),
        (typing.Literal["a", "b"], True),
        (_Color | None, True),
        (list[str], False),
        (str | list[str] | None, False),
        (dict, False),
        (typing.Any, False),
        (typing.Annotated[list[dict], "x"], False),
        # Unclassifiable → keep FastMCP's decode (the safe default).
        (typing.ForwardRef("Mystery"), False),
    ],
)
def test_is_text_only(annotation, expected):
    assert is_text_only(annotation) is expected


def test_install_leaves_every_advertised_schema_unchanged(monkeypatch):
    """Compare against STOCK FastMCP metadata (install disabled), not against
    an already-installed surface."""
    patched = {
        t.name: t.inputSchema
        for t in anyio.run(
            build_main_surface(scope_client=_NoopScopeClient()).list_tools
        )
    }
    monkeypatch.setattr(mcp_surface, "install_text_safe_pre_parse", lambda mcp: None)
    stock_mcp = build_main_surface(scope_client=_NoopScopeClient())
    assert not any(
        isinstance(t.fn_metadata, TextSafeFuncMetadata)
        for t in stock_mcp._tool_manager.list_tools()
    )
    stock = {t.name: t.inputSchema for t in anyio.run(stock_mcp.list_tools)}
    assert patched == stock
