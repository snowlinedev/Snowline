"""The MARQUEE end-to-end: the gateway aggregates the REAL governance plugin's
surfaces and a `record_decision` routed THROUGH the gateway actually reaches
governance and writes the real decision graph.

Unlike `test_gateway` (stub plugins) this wires the live
`snowline_governance.mcp_surface.build_main_surface` / `build_shadow_surface` as
the upstreams (over the in-memory connector — no HTTP needed for the unit test,
gateway.md pragmatics) and asserts BOTH:

  - the gateway's `main` surface lists governance's real decision + artifact
    tools (namespaced `governance.*`), and a routed `governance__record_decision`
    writes a decision the governance DB then returns;
  - the gateway's `shadow` surface carries governance's shadow tools + read-real
    grounding but NOT `governance__record_decision` — the isolation property, by
    composition.

Governance has its OWN database; this test provisions a disposable one and skips
cleanly if Postgres is unreachable (mirroring the governance conftest)."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import anyio
import pytest
import sqlalchemy as sa

from snowline_platform.gateway import SurfaceGateway
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry

from ._gateway_helpers import InMemoryConnector

# Governance test DB (separate from the platform's). Set BEFORE governance's DB
# layer builds its lazy engine.
_GOV_DB_URL = os.environ.get(
    "SNOWLINE_GOVERNANCE_TEST_DATABASE_URL",
    "postgresql+psycopg:///snowline_platform_gov_e2e_test",
)


def _maintenance_url(url: str) -> str:
    return str(sa.make_url(url).set(database="postgres"))


def _db_name(url: str) -> str:
    return sa.make_url(url).database


def _postgres_reachable() -> bool:
    try:
        eng = sa.create_engine(
            _maintenance_url(_GOV_DB_URL), isolation_level="AUTOCOMMIT"
        )
        with eng.connect():
            pass
        eng.dispose()
        return True
    except Exception:
        return False


def _recreate_db(url: str) -> None:
    name = _db_name(url)
    eng = sa.create_engine(_maintenance_url(url), isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ),
            {"n": name},
        )
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    eng.dispose()


class _StubScopeClient:
    """Minimal in-memory ScopeClient so governance needs no live platform: one
    scope `acme/widget`, resolving to a stable id + slug (the soft reference)."""

    def __init__(self) -> None:
        self._slug = "acme/widget"
        self._id = str(uuid.uuid5(uuid.NAMESPACE_URL, "scope:acme/widget"))

    def _row(self) -> dict:
        return {
            "id": self._id,
            "slug": self._slug,
            "name": "widget",
            "kind": "project",
            "status": "active",
            "isolated": False,
            "org": "acme",
        }

    def resolve(self, slug: str):
        return self._row() if slug == self._slug else None

    def ancestors(self, slug: str):
        return [self._row()] if slug == self._slug else []


@pytest.fixture(scope="module")
def governance_db():
    if not _postgres_reachable():
        pytest.skip(
            f"Postgres not reachable at {_maintenance_url(_GOV_DB_URL)!r} — "
            "governance end-to-end skipped"
        )
    os.environ["SNOWLINE_GOVERNANCE_DATABASE_URL"] = _GOV_DB_URL

    from alembic import command
    from alembic.config import Config

    from snowline_governance.db import reset_engine

    migrations = (
        Path(__import__("snowline_governance").__file__).resolve().parent
        / "migrations"
    )
    _recreate_db(_GOV_DB_URL)
    reset_engine()
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations))
    cfg.set_main_option("sqlalchemy.url", _GOV_DB_URL)
    command.upgrade(cfg, "head")
    yield _GOV_DB_URL
    reset_engine()


def _build_gov_surfaces():
    from snowline_governance.mcp_surface import (
        build_main_surface,
        build_shadow_surface,
    )

    client = _StubScopeClient()
    return build_main_surface(scope_client=client), build_shadow_surface(
        scope_client=client
    )


def _gateway_for(surface: str):
    main_srv, shadow_srv = _build_gov_surfaces()
    reg = PluginRegistry()
    reg.upsert(
        PluginManifest(
            name="governance",
            base_url="http://gov",
            surfaces={"/mcp": "main", "/shadow/mcp": "shadow"},
        )
    )
    connector = InMemoryConnector(
        {"http://gov/mcp": main_srv, "http://gov/shadow/mcp": shadow_srv}
    )
    return SurfaceGateway(reg, surface, connector)


def test_gateway_lists_real_governance_main_tools(governance_db):
    gw = _gateway_for("main")
    names = {t.name for t in anyio.run(gw.list_tools)}
    # Decision + artifact tools, all namespaced under the governance plugin.
    assert "governance__record_decision" in names
    assert "governance__supersede_decision" in names
    assert "governance__list_decisions" in names
    assert "governance__register_artifact" in names
    assert "governance__list_artifacts" in names


def test_record_decision_routes_through_gateway_to_governance(governance_db):
    gw = _gateway_for("main")
    res = anyio.run(
        gw.call_tool,
        "governance__record_decision",
        {"scope": "acme/widget", "decision": "use the gateway", "rationale": "spec #2"},
    )
    assert res.isError is not True, res.content
    payload = json.loads(res.content[0].text)
    decision_id = payload["id"]
    assert payload["decision"] == "use the gateway"

    # Prove it actually landed in governance's DB (not just echoed): read it back
    # through the gateway's get_decision route.
    read = anyio.run(gw.call_tool, "governance__get_decision", {"decision_id": decision_id})
    assert read.isError is not True
    back = json.loads(read.content[0].text)
    assert back["id"] == decision_id
    assert back["decision"] == "use the gateway"


def test_shadow_surface_excludes_real_write(governance_db):
    gw = _gateway_for("shadow")
    names = {t.name for t in anyio.run(gw.list_tools)}
    # Shadow write + read-real grounding present...
    assert "governance__create_branch" in names
    assert "governance__add_node" in names
    assert "governance__archive_branch" in names  # pure shadow op
    assert "governance__list_decisions" in names  # read-real grounding
    # ...but the real-write verb is ABSENT by composition (decision 8a7f0a11).
    assert "governance__record_decision" not in names
    assert "governance__supersede_decision" not in names
    assert "governance__register_artifact" not in names
    # branch-level graduation + rejection mint real decisions → main only, ABSENT
    # from shadow (the principal split, 99b92e1d).
    assert "governance__graduate_branch" not in names
    assert "governance__record_branch_rejection" not in names


def test_graduate_branch_routes_through_gateway_main_and_is_isolated(governance_db):
    """A whole-branch graduation routed THROUGH the gateway's main surface mints
    real decisions in governance's DB — and the verb is ABSENT from the shadow
    surface (the principal split, by composition over the wire)."""
    main_gw = _gateway_for("main")
    shadow_gw = _gateway_for("shadow")

    # graduate_branch is a real write → present on main, absent from shadow.
    main_names = {t.name for t in anyio.run(main_gw.list_tools)}
    shadow_names = {t.name for t in anyio.run(shadow_gw.list_tools)}
    assert "governance__graduate_branch" in main_names
    assert "governance__graduate_branch" not in shadow_names

    # Build a branch + a node on the SHADOW surface (the speculation half) …
    anyio.run(
        shadow_gw.call_tool,
        "governance__create_branch",
        {"scope": "acme/widget", "name": "wire-line", "narrative_notes": "explored"},
    )
    node_res = anyio.run(
        shadow_gw.call_tool,
        "governance__add_node",
        {"scope": "acme/widget", "name": "wire-line", "statement": "a kept node"},
    )
    node_id = json.loads(node_res.content[0].text)["id"]

    # … then graduate the whole branch on MAIN (the real write).
    grad = anyio.run(
        main_gw.call_tool,
        "governance__graduate_branch",
        {
            "scope": "acme/widget",
            "name": "wire-line",
            "end_statement": "adopt the wire line",
            "end_rationale": "synthesized",
            "include_node_ids": [node_id],
        },
    )
    assert grad.isError is not True, grad.content
    payload = json.loads(grad.content[0].text)
    assert payload["already_graduated"] is False
    assert payload["address"] == "acme/widget:wire-line"
    assert node_id in payload["promoted_node_ids"] or len(
        payload["promoted_node_ids"]
    ) == 1

    # Prove the END decision actually landed in governance's DB: read it back
    # through the gateway's get_decision route.
    read = anyio.run(
        main_gw.call_tool,
        "governance__get_decision",
        {"decision_id": payload["end_decision_id"]},
    )
    assert read.isError is not True
    back = json.loads(read.content[0].text)
    assert back["decision"] == "adopt the wire line"
    assert back["shadow_origin"]["label"] == "acme/widget:wire-line"
