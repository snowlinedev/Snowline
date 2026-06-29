"""The platform package must NOT import any monolith code (snowline_server /
snowline_substrate) — it's a fresh carve, not a re-export (spec §7, architecture
§2 dependency direction). Scans every source file for those imports."""

import ast
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "snowline_platform"
FORBIDDEN = ("snowline_server", "snowline_substrate")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_no_monolith_imports():
    offenders = {}
    for py in SRC.rglob("*.py"):
        bad = {
            m
            for m in _imported_modules(py)
            if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)
        }
        if bad:
            offenders[str(py)] = bad
    assert not offenders, f"monolith imports leaked into the platform: {offenders}"


def test_app_imports_clean():
    import snowline_platform.app  # noqa: F401
