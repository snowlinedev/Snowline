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
        "get_decision",
        "list_decisions",
        "applicable_decisions",
        # artifacts
        "register_artifact",
        "revise_artifact",
        "resolve_artifact",
        "get_artifact",
        "list_artifacts",
        "set_governs",
        "set_maturity",
        "applicable_artifacts",
    }
