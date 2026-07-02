"""The Claude Code memory importer — frontmatter parsing, type→kind mapping, and
idempotent upsert into the store.

The parse-only tests need no DB; `test_import_dir_*` are DB-backed (skip cleanly
without Postgres). The script is imported by file path (it lives under `scripts/`,
not the package).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SCRIPT = Path(__file__).parents[1] / "scripts" / "import_claude_memories.py"


def _load_importer():
    spec = importlib.util.spec_from_file_location("import_claude_memories", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


importer = _load_importer()


# --- parse-only (no DB) -----------------------------------------------------


def test_parse_frontmatter_flattens_nested_metadata_type():
    text = (FIXTURES / "verify-with-npm-build.md").read_text()
    fields, body = importer.parse_frontmatter(text)
    assert fields["name"] == "verify-with-npm-build"
    assert fields["metadata.type"] == "gotcha"
    assert body.startswith("The dashboard deploy build")
    assert "---" not in body


def test_parse_frontmatter_top_level_type():
    text = (FIXTURES / "mcp-is-default-surface.md").read_text()
    fields, body = importer.parse_frontmatter(text)
    assert fields["type"] == "project"
    assert body.startswith("Onboarding leads with MCP")


def test_parse_frontmatter_absent():
    text = (FIXTURES / "no-frontmatter.md").read_text()
    fields, body = importer.parse_frontmatter(text)
    assert fields == {}
    assert body == text


def test_map_kind_unknown_defaults_to_project():
    assert importer._map_kind("gotcha") == "gotcha"
    assert importer._map_kind("preference") == "user"  # synonym
    assert importer._map_kind("something-weird") == "project"
    assert importer._map_kind(None) == "project"


def test_dry_run_reports_without_writing():
    report = importer.import_dir(FIXTURES, dry_run=True)
    assert report["dry_run"] is True
    # MEMORY.md is skipped; the three real fixtures parse.
    assert set(report["created"]) == {
        "verify-with-npm-build",
        "mcp-is-default-surface",
        "no-frontmatter",  # no `name` frontmatter → falls back to the file stem
    }


# --- DB-backed round-trip ---------------------------------------------------


def test_import_dir_upserts_and_is_idempotent(clean_db):
    from snowline_memory import memory
    from snowline_memory.db import session_scope

    first = importer.import_dir(FIXTURES)
    assert len(first["created"]) == 3
    assert first["updated"] == []

    with session_scope() as s:
        out = memory.list_memories(s)
        assert out["items_total"] == 3
        # The nested-metadata fixture mapped type→kind.
        got = memory.recall(s, query="dashboard")
        assert got["memories"][0]["kind"] == "gotcha"

    # Re-run: every file upserts in place — updated, never duplicated.
    second = importer.import_dir(FIXTURES)
    assert second["created"] == []
    assert len(second["updated"]) == 3
    with session_scope() as s:
        assert memory.list_memories(s)["items_total"] == 3


def test_import_dir_tags_scope(clean_db):
    from snowline_memory import memory
    from snowline_memory.db import session_scope

    importer.import_dir(FIXTURES, scope="acme/widget")
    with session_scope() as s:
        out = memory.list_memories(s, scope="acme/widget")
        assert out["items_total"] == 3
        assert all(m["scope"] == "acme/widget" for m in out["memories"])


def test_main_rejects_non_directory(tmp_path):
    missing = tmp_path / "nope"
    assert importer.main([str(missing)]) == 2
