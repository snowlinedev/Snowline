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

Records are VALIDATED AT PARSE TIME (name grammar, scope grammar, kind
normalization — the same checks `remember` applies live), so `--dry-run`
predicts live outcomes instead of skipping exactly the validation that fails
live. Each record is applied under its own SAVEPOINT: one bad file is reported
`failed` (with its reason) and the rest still import. The per-file report is
ALWAYS printed (created / updated / skipped / FAILED) and the exit code is
nonzero when any file failed.

NOTE: the orchestrator runs this at deploy against the LIVE store. Do not run it
against the live store by hand; a `--dry-run` parses + validates + previews
(name / kind / description per file) without writing. No PyYAML dependency — a
small tolerant frontmatter parser handles the known shape (top-level scalars,
folded/literal block scalars, and a nested `metadata:` block).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The package import is safe for --dry-run: memory's engine is LAZY (db.py), so
# importing the validators touches no database. Only `db` is imported at apply
# time. Using the real validators here is the point — the parse phase applies
# exactly the validation `remember` applies live.
from snowline_memory import memory as memory_verbs

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

# YAML block-scalar indicators (`description: >-` etc.) — folded (`>`) joins
# continuation lines with spaces, literal (`|`) preserves newlines. The
# indicator itself is NEVER stored as the value.
_BLOCK_INDICATORS = {">", ">-", ">+", "|", "|-", "|+"}


def _map_kind(raw_type: str | None) -> str:
    if not raw_type:
        return DEFAULT_KIND
    return TYPE_TO_KIND.get(raw_type.strip().lower(), DEFAULT_KIND)


def _read_block_scalar(lines: list[str], i: int, indicator: str) -> tuple[str, int]:
    """Collect the continuation lines of a YAML block scalar whose `key: >-`
    (etc.) line is `lines[i]`. Returns `(value, next_index)`. Folded (`>`) joins
    lines with spaces (blank line ⇒ paragraph break); literal (`|`) preserves
    newlines. Trailing blank lines are chomped."""
    base_indent = len(lines[i]) - len(lines[i].lstrip())
    collected: list[str] = []
    j = i + 1
    while j < len(lines):
        nxt = lines[j]
        if nxt.strip() == "---":
            break
        if not nxt.strip():
            collected.append("")
            j += 1
            continue
        indent = len(nxt) - len(nxt.lstrip())
        if indent <= base_indent:
            break
        collected.append(nxt.strip())
        j += 1
    while collected and not collected[-1]:
        collected.pop()
    if indicator.startswith(">"):
        # Folded: join each paragraph's lines with spaces.
        paragraphs: list[list[str]] = [[]]
        for seg in collected:
            if seg:
                paragraphs[-1].append(seg)
            elif paragraphs[-1]:
                paragraphs.append([])
        value = "\n".join(" ".join(p) for p in paragraphs if p)
    else:
        value = "\n".join(collected)
    return value.strip(), j


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a `---`-delimited frontmatter block from the body. Returns
    `(fields, body)`. `fields` flattens a nested `metadata:` block as
    `metadata.type` (the one nested key we care about). Tolerant: a file with no
    frontmatter returns `({}, text)`.

    Deliberately minimal (no PyYAML): handles `key: value` scalars, the common
    folded/literal block scalars (`>-`, `>`, `|`, `|-` — continuation lines are
    joined/preserved, never the bare indicator), and a single level of
    indentation under `metadata:`. Quotes around plain values are stripped."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    body_start = None
    in_metadata = False
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            body_start = i + 1
            break
        if not line.strip():
            i += 1
            continue
        indented = line[:1].isspace()
        stripped = line.strip()
        if ":" not in stripped:
            i += 1
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if value in _BLOCK_INDICATORS:
            value, i = _read_block_scalar(lines, i, value)
        else:
            value = value.strip('"').strip("'")
            i += 1
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
    """Parse + VALIDATE one memory file into a `remember` kwargs dict, or None if
    it has no usable content. The resolved name (frontmatter `name`, else the
    file stem) is validated against the kebab grammar and `kind` is normalized
    HERE — the parse phase — so `--dry-run` fails exactly where a live run would.
    Raises `ValueError` (e.g. `InvalidNameError`) for a file that would fail
    live."""
    text = path.read_text(encoding="utf-8")
    fields, body = parse_frontmatter(text)
    content = body or text.strip()
    if not content:
        return None
    name = memory_verbs.validate_name(fields.get("name") or path.stem)
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
    Returns a report dict bucketing every file: `created` / `updated` /
    `skipped` (no content) / `failed` (`{file, error}` — parse/validation or
    apply errors; one bad file never aborts the batch). When `dry_run`, parses +
    validates + previews without writing (`preview` carries the per-file
    name/kind/description)."""
    scope = memory_verbs.validate_scope(scope)
    files = sorted(
        p for p in directory.glob("*.md") if p.name.lower() != "memory.md"
    )
    report: dict = {
        "created": [],
        "updated": [],
        "skipped": [],
        "failed": [],
        "preview": [],
        "dry_run": dry_run,
    }

    parsed: list[tuple[str, dict]] = []
    for path in files:
        try:
            rec = _record_from_file(path)
        except ValueError as exc:
            report["failed"].append({"file": path.name, "error": str(exc)})
            continue
        if rec is None:
            report["skipped"].append(path.name)
            continue
        parsed.append((path.name, rec))

    if dry_run:
        # Report what WOULD happen without touching the store — including the
        # parsed name/kind/description per file, so a mangled description is
        # visible by eyeball before a live run.
        for fname, rec in parsed:
            report["created"].append(rec["name"])
            report["preview"].append(
                {
                    "file": fname,
                    "name": rec["name"],
                    "kind": rec["kind"],
                    "description": rec["description"]
                    or memory_verbs._derive_description(rec["content"]),
                }
            )
        return report

    # Import lazily so --dry-run needs no DB / no engine.
    from snowline_memory.db import session_scope

    # ONE session, but a SAVEPOINT per record: a bad file rolls back to its
    # savepoint and lands in `failed` with its reason; the rest still commit.
    with session_scope() as session:
        for fname, rec in parsed:
            try:
                with session.begin_nested():
                    result = memory_verbs.remember(session, scope=scope, **rec)
            except Exception as exc:  # noqa: BLE001 — per-file isolation
                report["failed"].append({"file": fname, "error": str(exc)})
                continue
            bucket = "created" if result["created"] else "updated"
            report[bucket].append(result["name"])
    return report


def _print_report(report: dict, directory: Path) -> None:
    tag = "DRY-RUN (no writes) " if report["dry_run"] else ""
    print(f"{tag}memory import from {directory}")
    if report["dry_run"]:
        for p in report["preview"]:
            print(f"  would import: {p['file']} -> {p['name']} [kind={p['kind']}]")
            print(f"      description: {p['description']}")
    else:
        for name in report["created"]:
            print(f"  created: {name}")
        for name in report["updated"]:
            print(f"  updated: {name}")
    for name in report["skipped"]:
        print(f"  skipped (no content): {name}")
    for f in report["failed"]:
        print(f"  FAILED: {f['file']} — {f['error']}")
    print(
        f"summary: {len(report['created'])} created/would-import, "
        f"{len(report['updated'])} updated, {len(report['skipped'])} skipped, "
        f"{len(report['failed'])} failed"
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
        help="parse + validate + preview without writing to the store",
    )
    args = parser.parse_args(argv)
    if not args.directory.is_dir():
        print(f"not a directory: {args.directory}", file=sys.stderr)
        return 2
    try:
        report = import_dir(args.directory, scope=args.scope, dry_run=args.dry_run)
    except memory_verbs.InvalidScopeError as exc:
        print(f"invalid --scope: {exc}", file=sys.stderr)
        return 2
    _print_report(report, args.directory)
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
