"""Memory behavior — remember upsert, name/scope validation, recall FTS + filters,
digest shape/grouping, list, forget.

DB-backed (skips cleanly when Postgres is unavailable). Exercises the real
generated `tsvector` column + GIN index the migration builds.
"""

from __future__ import annotations

import pytest

from snowline_memory import memory


# --- remember: upsert, generation, derivation -------------------------------


def test_remember_insert_returns_full_row(db_session):
    row = memory.remember(
        db_session,
        content="Always run `uv run pytest -q` before pushing.",
        name="run-tests-before-push",
        description="run the suite before pushing",
        kind="gotcha",
        scope="acme/widget",
    )
    assert row["created"] is True
    assert row["name"] == "run-tests-before-push"
    assert row["description"] == "run the suite before pushing"
    assert row["kind"] == "gotcha"
    assert row["scope"] == "acme/widget"
    assert row["id"]


def test_remember_upserts_by_name_in_place(db_session):
    first = memory.remember(
        db_session, content="old content", name="focus", kind="project"
    )
    assert first["created"] is True

    second = memory.remember(
        db_session, content="new content", name="focus", kind="user"
    )
    assert second["created"] is False  # updated in place, not inserted
    assert second["content"] == "new content"
    assert second["kind"] == "user"

    listed = memory.list_memories(db_session)
    assert listed["items_total"] == 1  # ONE row — upsert, not duplicate
    assert listed["memories"][0]["name"] == "focus"


def test_remember_generates_name_from_description(db_session):
    row = memory.remember(
        db_session,
        content="body text here",
        description="Prefer psycopg over psycopg2 everywhere",
    )
    assert row["name"] == "prefer-psycopg-over-psycopg2-everywhere"


def test_remember_derives_description_from_content(db_session):
    row = memory.remember(
        db_session,
        content="# The dashboard build is tsc -b && vite build\nmore detail",
        name="dashboard-build",
    )
    assert row["description"] == "The dashboard build is tsc -b && vite build"


def test_remember_defaults_kind_to_project(db_session):
    row = memory.remember(db_session, content="x", name="thing")
    assert row["kind"] == "project"


def test_remember_normalizes_kind_case(db_session):
    # `kind` is lowercased at the boundary — "Gotcha" and "gotcha" must not
    # split the digest into two groups.
    row = memory.remember(db_session, content="x", name="cased-kind", kind="Gotcha")
    assert row["kind"] == "gotcha"

    memory.remember(db_session, content="y", name="lower-kind", kind="gotcha")
    out = memory.memory_digest(db_session)
    assert [g["kind"] for g in out["groups"]] == ["gotcha"]  # ONE group
    assert len(out["groups"][0]["entries"]) == 2


def test_remember_identical_upsert_bumps_updated_at(clean_db):
    # Spec §4: `updated_at` bumps on EVERY upsert — including an
    # identical-content re-remember (SQLAlchemy would otherwise skip the no-op
    # UPDATE and leave it stale). Separate transactions, as in production (one
    # per MCP tool call), since now() is transaction-time.
    from datetime import datetime

    from snowline_memory.db import session_scope

    with session_scope() as s:
        first = memory.remember(s, content="same content", name="touched")
    with session_scope() as s:
        second = memory.remember(s, content="same content", name="touched")

    assert second["created"] is False
    assert datetime.fromisoformat(second["updated_at"]) > datetime.fromisoformat(
        first["updated_at"]
    )
    assert second["created_at"] == first["created_at"]  # insert time is stable


def test_remember_rejects_blank_content(db_session):
    with pytest.raises(ValueError):
        memory.remember(db_session, content="   ", name="x")


def test_remember_rejects_bad_name(db_session):
    with pytest.raises(memory.InvalidNameError):
        memory.remember(db_session, content="x", name="Not Kebab Case")


def test_remember_rejects_bad_scope(db_session):
    with pytest.raises(memory.InvalidScopeError):
        memory.remember(db_session, content="x", name="y", scope="Bad Scope!")


def test_remember_stores_scope_verbatim(db_session):
    row = memory.remember(
        db_session, content="x", name="y", scope="acme/widget-v3"
    )
    assert row["scope"] == "acme/widget-v3"


# --- recall: FTS ranking + filters ------------------------------------------


def test_recall_fts_ranks_relevant_first(db_session):
    memory.remember(
        db_session,
        content="Postgres is the durable store for all Snowline data.",
        name="postgres-store",
    )
    memory.remember(
        db_session,
        content="The dashboard is a Vite React app.",
        name="dashboard-stack",
    )
    out = memory.recall(db_session, query="postgres")
    assert out["items_total"] == 1
    assert out["memories"][0]["name"] == "postgres-store"
    assert "rank" in out["memories"][0]


def test_recall_no_query_is_newest_first(clean_db):
    # Production-faithful: each `remember` is its OWN transaction (one per MCP
    # tool call), so the two rows get distinct `updated_at` (now() is
    # transaction-time). newest-first then orders the later write ahead.
    from snowline_memory.db import session_scope

    with session_scope() as s:
        memory.remember(s, content="a", name="first")
    with session_scope() as s:
        memory.remember(s, content="b", name="second")
    with session_scope() as s:
        out = memory.recall(s)
    names = [m["name"] for m in out["memories"]]
    assert names[0] == "second"  # most-recently-updated first
    assert out["items_total"] == 2


def test_recall_filters_by_kind(db_session):
    memory.remember(db_session, content="a", name="pref", kind="user")
    memory.remember(db_session, content="b", name="conv", kind="project")
    out = memory.recall(db_session, kind="user")
    assert out["items_total"] == 1
    assert out["memories"][0]["name"] == "pref"


def test_recall_scope_includes_portfolio_wide(db_session):
    memory.remember(db_session, content="scoped", name="scoped", scope="acme/widget")
    memory.remember(db_session, content="global", name="global")  # portfolio-wide
    memory.remember(db_session, content="other", name="other", scope="acme/gadget")

    out = memory.recall(db_session, scope="acme/widget")
    names = {m["name"] for m in out["memories"]}
    assert names == {"scoped", "global"}  # scope's own + portfolio-wide, not other


# --- digest: shape + grouping -----------------------------------------------


def test_digest_groups_by_kind_in_soft_enum_order(db_session):
    memory.remember(db_session, content="a", name="a-gotcha", kind="gotcha")
    memory.remember(db_session, content="b", name="b-user", kind="user")
    memory.remember(db_session, content="c", name="c-project", kind="project")

    out = memory.memory_digest(db_session)
    assert out["items_total"] == 3
    kinds = [g["kind"] for g in out["groups"]]
    # Soft-enum order: user before project before gotcha.
    assert kinds == ["user", "project", "gotcha"]
    entries = out["groups"][0]["entries"]
    assert entries[0]["name"] == "b-user"
    assert "description" in entries[0]


def test_digest_scope_includes_portfolio_wide(db_session):
    memory.remember(db_session, content="s", name="scoped", scope="acme/widget")
    memory.remember(db_session, content="g", name="global")
    memory.remember(db_session, content="o", name="other", scope="acme/gadget")

    out = memory.memory_digest(db_session, scope="acme/widget")
    names = {e["name"] for g in out["groups"] for e in g["entries"]}
    assert names == {"scoped", "global"}


def test_digest_novel_kind_sorts_after_soft_enum(db_session):
    memory.remember(db_session, content="a", name="known", kind="user")
    memory.remember(db_session, content="b", name="weird", kind="zzz-custom")
    out = memory.memory_digest(db_session)
    kinds = [g["kind"] for g in out["groups"]]
    assert kinds == ["user", "zzz-custom"]  # novel kind after the soft enum


# --- model/migration index parity (no DB) ------------------------------------


def test_model_declares_the_migration_indexes():
    # The model's __table_args__ must declare the same indexes (names and all)
    # the genesis migration creates, so autogenerate can't propose dropping them.
    from snowline_memory.models import Memory

    idx = {i.name: i for i in Memory.__table__.indexes}
    assert set(idx) == {"ix_memories_scope_slug", "ix_memories_search_vector"}
    assert [c.name for c in idx["ix_memories_scope_slug"].columns] == ["scope_slug"]
    gin = idx["ix_memories_search_vector"]
    assert [c.name for c in gin.columns] == ["search_vector"]
    assert gin.dialect_options["postgresql"]["using"] == "gin"


# --- list + forget ----------------------------------------------------------


def test_list_memories_returns_headers(db_session):
    memory.remember(db_session, content="body", name="thing", description="a hook")
    out = memory.list_memories(db_session)
    hdr = out["memories"][0]
    assert hdr["name"] == "thing"
    assert hdr["description"] == "a hook"
    assert "content" not in hdr  # headers omit the body


def test_forget_removes_and_is_idempotent(db_session):
    memory.remember(db_session, content="x", name="doomed")
    gone = memory.forget(db_session, "doomed")
    assert gone == {"forgotten": True, "name": "doomed"}
    assert memory.list_memories(db_session)["items_total"] == 0

    # Idempotent — forgetting a missing name is a no-op, not an error.
    again = memory.forget(db_session, "doomed")
    assert again == {"forgotten": False, "name": "doomed"}
