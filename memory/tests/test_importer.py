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


def test_parse_frontmatter_folded_description():
    text = (
        "---\n"
        "name: folded-note\n"
        "description: >-\n"
        "  the first folded line\n"
        "  and the second line\n"
        "metadata:\n"
        "  type: gotcha\n"
        "---\n"
        "body\n"
    )
    fields, body = importer.parse_frontmatter(text)
    assert fields["description"] == "the first folded line and the second line"
    assert fields["metadata.type"] == "gotcha"
    assert body == "body"


def test_parse_frontmatter_literal_description_preserves_newlines():
    text = "---\ndescription: |\n  line one\n  line two\n---\nbody\n"
    fields, _ = importer.parse_frontmatter(text)
    assert fields["description"] == "line one\nline two"


def test_parse_frontmatter_never_stores_bare_block_indicator():
    # An empty block scalar parses to "" — `_record_from_file` then treats the
    # description as absent and `remember` derives one from the content; the
    # literal ">-" must never be stored.
    for ind in (">", ">-", "|", "|-"):
        text = f"---\ndescription: {ind}\n---\nbody\n"
        fields, _ = importer.parse_frontmatter(text)
        assert fields["description"] == ""


# --- validating dry-run -------------------------------------------------------


def test_dry_run_validates_names_and_previews(tmp_path):
    (tmp_path / "good-note.md").write_text(
        "---\nname: good-note\ndescription: a hook\nmetadata:\n  type: gotcha\n"
        "---\nbody text\n"
    )
    # No frontmatter name → falls back to the file stem, auto-normalized to
    # kebab-case (issue #48) the same way a live `remember` would; an
    # all-punctuation stem normalizes to "" and still fails.
    (tmp_path / "!!!.md").write_text("some content\n")

    report = importer.import_dir(tmp_path, dry_run=True)
    assert report["created"] == ["good-note"]
    assert [f["file"] for f in report["failed"]] == ["!!!.md"]
    assert "invalid memory name" in report["failed"][0]["error"]
    assert report["preview"] == [
        {
            "file": "good-note.md",
            "name": "good-note",
            "kind": "gotcha",
            "description": "a hook",
        }
    ]


def test_dry_run_preview_derives_missing_description(tmp_path):
    (tmp_path / "derived-note.md").write_text("# First line becomes the hook\nrest\n")
    report = importer.import_dir(tmp_path, dry_run=True)
    (preview,) = report["preview"]
    assert preview["description"] == "First line becomes the hook"


def test_dry_run_validates_scope():
    from snowline_memory.memory import InvalidScopeError

    with pytest.raises(InvalidScopeError):
        importer.import_dir(FIXTURES, scope="Bad Scope!", dry_run=True)


def test_main_exits_nonzero_and_always_prints_report(tmp_path, capsys):
    (tmp_path / "good-note.md").write_text("good content\n")
    (tmp_path / "!!!.md").write_text("content\n")

    rc = importer.main([str(tmp_path), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 1  # any failed file → nonzero exit
    assert "would import: good-note.md -> good-note" in out
    assert "description: good content" in out
    assert "FAILED: !!!.md" in out
    assert "1 failed" in out


def test_main_rejects_bad_scope(tmp_path, capsys):
    (tmp_path / "x-note.md").write_text("content\n")
    rc = importer.main([str(tmp_path), "--scope", "Bad Scope!", "--dry-run"])
    assert rc == 2
    assert "invalid --scope" in capsys.readouterr().err


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


def test_import_dir_isolates_per_record_failures(clean_db, tmp_path, monkeypatch):
    """One record failing AT APPLY TIME rolls back to its savepoint and lands in
    `failed`; the other records still commit."""
    from snowline_memory import memory
    from snowline_memory.db import session_scope

    (tmp_path / "aa-good-one.md").write_text("first good note\n")
    (tmp_path / "poison.md").write_text("poison note\n")
    (tmp_path / "zz-good-two.md").write_text("second good note\n")

    real_remember = memory.remember

    def exploding(session, **kwargs):
        if kwargs.get("name") == "poison":
            raise RuntimeError("boom at apply time")
        return real_remember(session, **kwargs)

    monkeypatch.setattr(importer.memory_verbs, "remember", exploding)

    report = importer.import_dir(tmp_path)
    assert report["created"] == ["aa-good-one", "zz-good-two"]
    assert report["failed"] == [
        {"file": "poison.md", "error": "boom at apply time"}
    ]

    # The good rows actually persisted — the batch was not aborted.
    with session_scope() as s:
        out = memory.list_memories(s)
        assert {m["name"] for m in out["memories"]} == {"aa-good-one", "zz-good-two"}


def test_import_dir_live_parse_failure_doesnt_abort_batch(clean_db, tmp_path):
    """A file that fails PARSE-phase validation (all-punctuation stem → empty
    after kebab-normalization) is reported `failed`; the rest of the batch still
    imports live."""
    from snowline_memory import memory
    from snowline_memory.db import session_scope

    (tmp_path / "good-note.md").write_text("good content\n")
    (tmp_path / "!!!.md").write_text("content\n")

    report = importer.import_dir(tmp_path)
    assert report["created"] == ["good-note"]
    assert [f["file"] for f in report["failed"]] == ["!!!.md"]
    with session_scope() as s:
        assert memory.list_memories(s)["items_total"] == 1


def test_main_rejects_non_directory(tmp_path):
    missing = tmp_path / "nope"
    assert importer.main([str(missing)]) == 2
