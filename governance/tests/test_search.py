"""Real-graph corpus search (#170) — full-text over current artifact versions
+ current decision leaves, ranked and merged, scope-narrowable; superseded
artifacts/decisions and historical versions never match.

DB-backed (skips cleanly when Postgres is unavailable); no scope service needed
(the service takes a pre-resolved scope_id — the MCP surface resolves slugs).
"""

from __future__ import annotations

import uuid

import pytest

from snowline_governance import artifacts, decisions, search


def _sid(slug: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


def _scope_row(slug: str) -> dict:
    return {"id": str(_sid(slug)), "slug": slug}


def test_search_finds_artifacts_and_decisions_ranked(db_session):
    art = artifacts.register_artifact(
        db_session, body="# Sync engine spec\nConflict resolution uses vector clocks."
    )
    dec = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"),
        "resolve sync conflicts with vector clocks", "deterministic merge",
    )
    artifacts.register_artifact(db_session, body="# Unrelated doc\nnothing here")

    out = search.corpus_search(db_session, "vector clocks")
    kinds = {(r["kind"], r["id"]) for r in out["results"]}
    assert ("artifact", art["id"]) in kinds
    assert ("decision", dec["id"]) in kinds
    assert out["results_total"] == 2
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id[art["id"]]["title"] == "Sync engine spec"
    assert by_id[dec["id"]]["scope"] == "acme/widget"
    assert all(r["snippet"] and r["rank"] > 0 for r in out["results"])


def test_search_matches_only_current_versions(db_session):
    """A revised-away body stops matching; the new body matches — search never
    resurrects historical prose."""
    art = artifacts.register_artifact(
        db_session, body="# Auth spec\nthe old oauth flow"
    )
    artifacts.revise_artifact(
        db_session, art["id"], "refines",
        body_snapshot="# Auth spec\nthe new passkey flow",
    )
    assert search.corpus_search(db_session, "oauth")["results"] == []
    hits = search.corpus_search(db_session, "passkey")["results"]
    assert [r["id"] for r in hits] == [art["id"]]


def test_search_excludes_superseded_artifacts_and_decisions(db_session):
    old = artifacts.register_artifact(
        db_session, body="# Early spec\nzanzibar authorization"
    )
    survivor = artifacts.register_artifact(
        db_session, body="# Consolidated spec\nzanzibar authorization, digested"
    )
    artifacts.supersede_artifact(db_session, old["id"], survivor["id"])

    d1 = decisions.record_decision(
        db_session, "acme/widget", _sid("acme/widget"), "use zanzibar for authz"
    )
    decisions.supersede_decision(
        db_session, d1["id"], "use plain RBAC instead of zanzibar"
    )

    out = search.corpus_search(db_session, "zanzibar")
    ids = {r["id"] for r in out["results"]}
    assert old["id"] not in ids and d1["id"] not in ids
    assert survivor["id"] in ids
    assert out["results_total"] == 2  # the survivor + the superseding leaf


def test_search_scope_narrowing(db_session):
    widget = artifacts.register_artifact(
        db_session, body="# Widget telemetry\nmetrics pipeline",
        governs="acme/widget", resolved_scopes={"acme/widget": _scope_row("acme/widget")},
    )
    artifacts.register_artifact(
        db_session, body="# Other telemetry\nmetrics pipeline",
        governs="acme/other", resolved_scopes={"acme/other": _scope_row("acme/other")},
    )
    everywhere = artifacts.register_artifact(
        db_session, body="# Telemetry conventions\nmetrics pipeline", governs="*"
    )
    decisions.record_decision(
        db_session, "acme/other", _sid("acme/other"), "metrics pipeline is kafka"
    )

    out = search.corpus_search(
        db_session, "metrics pipeline",
        scope_id=_sid("acme/widget"), scope_slug="acme/widget",
    )
    assert out["scope"] == "acme/widget"
    ids = {r["id"] for r in out["results"]}
    # The widget artifact + the governs_all artifact; the other-scope artifact
    # and other-scope decision are excluded.
    assert ids == {widget["id"], everywhere["id"]}


def test_search_governs_labels_ride_the_rows(db_session):
    artifacts.register_artifact(
        db_session, body="# Labeled\nsearchable prose",
        governs="acme/widget", resolved_scopes={"acme/widget": _scope_row("acme/widget")},
    )
    row = search.corpus_search(db_session, "searchable prose")["results"][0]
    assert row["governs"] == ["acme/widget"]
    assert row["governs_all"] is False


def test_search_blank_query_raises(db_session):
    with pytest.raises(ValueError, match="non-empty"):
        search.corpus_search(db_session, "   ")


def test_search_limit_caps_results_not_total(db_session):
    for i in range(5):
        artifacts.register_artifact(
            db_session, body=f"# Doc {i}\ncommon needle phrase"
        )
    out = search.corpus_search(db_session, "needle phrase", limit=2)
    assert len(out["results"]) == 2
    assert out["results_total"] == 5
