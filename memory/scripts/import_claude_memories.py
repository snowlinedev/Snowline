#!/usr/bin/env python
"""Import existing Claude Code memory markdown files into the memory store.

The base Claude Code memory system keeps per-project memories as markdown files
with YAML-ish frontmatter (`name`, `description`, `metadata.type`) and a markdown
body. This script ingests a DIRECTORY of those files and upserts each via the
same `remember` semantics the MCP surface uses, so day-one migration
(~40 memories) is one command.

  python -m scripts.import_claude_memories <dir> [--scope <slug>] [--dry-run]

It is IDEMPOTENT: `remember` upserts by `name`, so re-running updates in place and
never duplicates. `type -> kind` maps onto the soft enum (unknown → `project`).
Prints a per-file report (created / updated / skipped) and a summary.

NOTE: the orchestrator runs this at deploy against the LIVE store. Do not run it
against the live store by hand; a `--dry-run` parses + reports without writing.
No PyYAML dependency — a small tolerant frontmatter parser handles the known
shape (top-level scalars + a nested `metadata:` block).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# `type` (Claude Code) -> `kind` (memory soft enum). Unknown types fall through
# to the default in `_map_kind`.
TYPE_TO_KIND = {
    "user": "user",
    "feedback": "feedback",
    "project": "project",
    "reference": "reference",
    "gotcha": "gotcha",
    # A few plausible synonyms seen in the wild map onto the soft enum.
    "preference": "user",
    "note": "project",
    "doc": "reference",
}
DEFAULT_KIND = "project"


def _map_kind(raw_type: str | None) -> str:
    if not raw_type:
        return DEFAULT_KIND
    return TYPE_TO_KIND.get(raw_type.strip().lower(), DEFAULT_KIND)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a `---`-delimited frontmatter block from the body. Returns
    `(fields, body)`. `fields` flattens a nested `metadata:` block as
    `metadata.type` (the one nested key we care about). Tolerant: a file with no
    frontmatter returns `({}, text)`.

    Deliberately minimal (no PyYAML): handles `key: value` scalars and a single
    level of indentation under `metadata:`. Quotes around values are stripped."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    body_start = None
    in_metadata = False
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            body_start = i + 1
            break
        if not line.strip():
            continue
        indented = line[:1].isspace()
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key == "metadata" and not value:
            in_metadata = True
            continue
        if in_metadata and indented:
            fields[f"metadata.{key}"] = value
            continue
        in_metadata = False
        fields[key] = value
    body = "\n".join(lines[body_start:]).strip() if body_start is not None else ""
    return fields, body


def _record_from_file(path: Path) -> dict | None:
    """Parse one memory file into a `remember` kwargs dict, or None if it has no
    usable content."""
    text = path.read_text(encoding="utf-8")
    fields, body = parse_frontmatter(text)
    content = body or text.strip()
    if not content:
        return None
    name = fields.get("name") or path.stem
    return {
        "name": name,
        "description": fields.get("description") or None,
        "kind": _map_kind(fields.get("metadata.type") or fields.get("type")),
        "content": content,
    }


def import_dir(
    directory: Path,
    scope: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Import every `*.md` (except `MEMORY.md`, the index) under `directory`.
    Returns a report dict. When `dry_run`, parses + reports without writing."""
    files = sorted(
        p for p in directory.glob("*.md") if p.name.lower() != "memory.md"
    )
    report = {"created": [], "updated": [], "skipped": [], "dry_run": dry_run}

    def _apply(records: list[dict]) -> None:
        # Import lazily so `--dry-run` needs no DB / no engine.
        from snowline_memory import memory
        from snowline_memory.db import session_scope

        with session_scope() as session:
            for rec in records:
                result = memory.remember(session, scope=scope, **rec)
                bucket = "created" if result["created"] else "updated"
                report[bucket].append(result["name"])

    parsed: list[dict] = []
    for path in files:
        rec = _record_from_file(path)
        if rec is None:
            report["skipped"].append(path.name)
            continue
        parsed.append(rec)

    if dry_run:
        # Report what WOULD happen without touching the store.
        report["created"] = [r["name"] for r in parsed]
    else:
        _apply(parsed)
    return report


def _print_report(report: dict, directory: Path) -> None:
    tag = "DRY-RUN (no writes) " if report["dry_run"] else ""
    print(f"{tag}memory import from {directory}")
    if report["dry_run"]:
        for name in report["created"]:
            print(f"  would import: {name}")
    else:
        for name in report["created"]:
            print(f"  created: {name}")
        for name in report["updated"]:
            print(f"  updated: {name}")
    for name in report["skipped"]:
        print(f"  skipped (no content): {name}")
    print(
        f"summary: {len(report['created'])} created/parsed, "
        f"{len(report['updated'])} updated, {len(report['skipped'])} skipped"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="dir of memory *.md files")
    parser.add_argument(
        "--scope", default=None, help="tag every imported memory with this slug"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse + report without writing to the store",
    )
    args = parser.parse_args(argv)
    if not args.directory.is_dir():
        print(f"not a directory: {args.directory}", file=sys.stderr)
        return 2
    report = import_dir(args.directory, scope=args.scope, dry_run=args.dry_run)
    _print_report(report, args.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
