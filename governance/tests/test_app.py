"""The governance app — health endpoint, the mounted MCP surface, and that the
`main` surface carries the 5 decision tools. No DB / no platform needed
(migrate + register disabled; a stub scope client injected)."""

from __future__ import annotations

import anyio
from starlette.testclient import TestClient

from snowline_governance.app import create_app
from snowline_governance.mcp_surface import build_main_surface


class _NoopScopeClient:
    def resolve(self, slug: str): return None
    def ancestors(self, slug: str): return []


def test_health_ok():
    app = create_app(
        scope_client=_NoopScopeClient(),
        migrate_on_startup=False,
        register_on_startup=False,
    )
    # Don't enter the lifespan (it runs the MCP session manager); hit /health,
    # which is a plain route on the app.
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["plugin"] == "governance"


def test_main_surface_exposes_the_decision_and_artifact_tools():
    surface = build_main_surface(scope_client=_NoopScopeClient())
    tools = {t.name for t in anyio.run(surface.list_tools)}
    assert tools == {
        # decisions
        "record_decision",
        "supersede_decision",
        # §6.1's second reconciliation path — mark a false-positive pair
        # compatible so both decisions stand (replication-continuity, #97).
        "mark_decisions_compatible",
        "get_decision",
        "list_decisions",
        "applicable_decisions",
        # §6.1 unreconciled view (replication-continuity, #79) — a read on the
        # shared read set (registered on BOTH surfaces).
        "unreconciled_decisions",
        # graduation (shadow → real) — real writes, main-only (decision 99b92e1d).
        # Node-level + branch-level (graduate_branch / record_branch_rejection).
        "graduate",
        "graduate_branch",
        "record_branch_rejection",
        # artifacts
        "register_artifact",
        "revise_artifact",
        "resolve_artifact",
        # artifact-level retirement (#166/#174) — with and without a successor.
        "supersede_artifact",
        "retire_artifact",
        "get_artifact",
        "get_artifact_version",
        "list_artifacts",
        "list_artifact_versions",
        "set_governs",
        "set_maturity",
        # structured code anchors (#172) — the spec↔code map write.
        "set_code_refs",
        "applicable_artifacts",
        # real-graph corpus search (#170) — shared read set, both surfaces.
        "governance_corpus_search",
        # the reverse code-map read (#172) — shared read set, both surfaces.
        "artifacts_for_path",
    }


def test_polymorphic_tool_params_advertise_typed_schemas():
    """Feedback e9ac7680: untyped tool params surface as schema-less, so
    clients pass JSON-encoded strings that only fail server-side. The
    polymorphic params must advertise their real shapes — `code_refs` an array
    of objects, `governs` a string-or-array — so malformed input is caught at
    the schema layer."""
    surface = build_main_surface(scope_client=_NoopScopeClient())
    schemas = {
        t.name: t.inputSchema for t in anyio.run(surface.list_tools)
    }

    def _types(prop: dict) -> set[str]:
        if "type" in prop:
            return {prop["type"]}
        return {
            alt["type"]
            for alt in prop.get("anyOf", [])
            if isinstance(alt, dict) and "type" in alt
        }

    code_refs = schemas["set_code_refs"]["properties"]["code_refs"]
    assert "array" in _types(code_refs)
    assert "string" not in _types(code_refs)
    for tool in ("set_governs", "register_artifact"):
        governs = schemas[tool]["properties"]["governs"]
        assert {"string", "array"} <= _types(governs)
