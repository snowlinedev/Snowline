"""Import-purity: `snowline_governance` imports NO monolith code
(`snowline_server` / `snowline_substrate`) and NO platform INTERNALS
(`snowline_platform`) — it talks to the platform only over HTTP
(governance-plugin spec §10, architecture §2 dependency direction).

The decision logic here is a CARVE (re-written), not a re-export; the scope
dependency and registration are HTTP, not Python imports.
"""

import ast
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "snowline_governance"
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
    assert not offenders, f"forbidden imports leaked into governance: {offenders}"


def test_app_imports_clean():
    import snowline_governance.app  # noqa: F401
