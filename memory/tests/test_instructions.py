"""Size + consistency guard for the memory surface's MCP `instructions`
(Snowline#263)."""

from __future__ import annotations

import re

import anyio

from snowline_memory.mcp_surface import _INSTRUCTIONS, build_main_surface


def test_memory_instructions_size_and_content():
    assert len(_INSTRUCTIONS.encode("utf-8")) <= 800
    assert "memory_digest" in _INSTRUCTIONS
    assert "recall" in _INSTRUCTIONS
    tools = {t.name for t in anyio.run(build_main_surface().list_tools)}
    assert set(re.findall(r"`([a-z_]+)(?:\(|`)", _INSTRUCTIONS)) <= tools
