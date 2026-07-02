"""Import-purity: `snowline_memory` imports NO monolith code (`snowline_server` /
`snowline_substrate`) and NO platform INTERNALS (`snowline_platform`) — it talks
to the platform only over HTTP (architecture §2 dependency direction, mirroring
governance).

Scopes are platform-owned; memory references a slug as a soft, verbatim-stored
reference (the slug grammar is CARRIED, not imported).
"""

import ast
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "snowline_memory"
FORBIDDEN = ("snowline_server", "snowline_substrate", "snowline_platform")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_no_monolith_or_platform_imports():
    offenders = {}
    for py in SRC.rglob("*.py"):
        bad = {
            m
            for m in _imported_modules(py)
            if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)
        }
        if bad:
            offenders[str(py)] = bad
    assert not offenders, f"forbidden imports leaked into memory: {offenders}"


def test_app_imports_clean():
    import snowline_memory.app  # noqa: F401
