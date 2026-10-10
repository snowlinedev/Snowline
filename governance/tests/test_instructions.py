"""Size + consistency guard for the governance surfaces' MCP `instructions`
(Snowline#263): they are injected into every session, so they stay terse and
never reference a tool that is not registered on the surface."""

from __future__ import annotations

import re

import anyio

from snowline_governance.mcp_surface import (
    _MAIN_INSTRUCTIONS,
    _SHADOW_INSTRUCTIONS,
    build_main_surface,
    build_shadow_surface,
)


class _NoopScopeClient:
    def resolve(self, slug: str): return None
    def ancestors(self, slug: str): return []


def _names(surface) -> set[str]:
    return {t.name for t in anyio.run(surface.list_tools)}


def _mentioned(text: str) -> set[str]:
    return set(re.findall(r"`([a-z_]+)(?:\(|`)", text)) - {"applicable_", "main"}


def test_main_instructions_size_and_content():
    assert len(_MAIN_INSTRUCTIONS.encode("utf-8")) <= 1228
    assert "record_decision" in _MAIN_INSTRUCTIONS
    assert "applicable_decisions" in _MAIN_INSTRUCTIONS
    tools = _names(build_main_surface(scope_client=_NoopScopeClient()))
    assert _mentioned(_MAIN_INSTRUCTIONS) <= tools


def test_shadow_instructions_size_and_content():
    assert len(_SHADOW_INSTRUCTIONS.encode("utf-8")) <= 400
    tools = _names(build_shadow_surface(scope_client=_NoopScopeClient()))
    assert _mentioned(_SHADOW_INSTRUCTIONS) <= tools
