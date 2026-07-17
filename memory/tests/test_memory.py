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


def test_remember_normalizes_underscore_name(db_session):
    # Issue #48: a provided name is auto-kebab-cased, not hard-rejected — an
    # underscore-named note (as produced by the bulk-migration importer) saves
    # on the first attempt.
    row = memory.remember(db_session, content="x", name="my_note")
    assert row["name"] == "my-note"


def test_remember_normalizes_sentence_case_with_spaces(db_session):
    row = memory.remember(db_session, content="x", name="My Note Title")
    assert row["name"] == "my-note-title"


def test_remember_treats_whitespace_only_name_as_omitted(db_session):
    # "   " means "no name", same as None/"" — falls through to the generated
    # name instead of raising on the empty normalization.
    row = memory.remember(db_session, content="A real note\nbody", name="   ")
    assert row["name"] == "a-real-note"


def test_remember_normalized_name_upserts_across_spellings(db_session):
    # Normalization happens BEFORE the upsert lookup, so `my_note` and
    # `my-note` resolve to the same stored key — one row, not two.
    first = memory.remember(db_session, content="old", name="my_note")
    assert first["created"] is True

    second = memory.remember(db_session, content="new", name="my-note")
    assert second["created"] is False
    assert second["name"] == "my-note"

    listed = memory.list_memories(db_session)
    assert listed["items_total"] == 1


def test_remember_rejects_all_punctuation_name(db_session):
    # Normalization strips every character, leaving "" — that's the one case
    # that still raises, with a message stating the rule exactly.
    with pytest.raises(memory.InvalidNameError, match="kebab-case"):
        memory.remember(db_session, content="x", name="!!!___")


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
    assert out["match_mode"] == "all_terms"


def test_recall_multiword_relaxes_to_any_term(db_session):
    """#133 (production-faithful repro): a natural multi-word query whose terms
    DON'T all appear in one memory must not return 0 — the strict all-terms
    query falls back to any-term, ranked best-first, and says so."""
    memory.remember(
        db_session,
        name="walkthrough-plugin-usage",
        description="How the walkthrough plugin drives a simulator session",
        content="Drive walkthroughs via the walkthrough plugin's tap/swipe tools.",
    )
    memory.remember(
        db_session,
        name="dashboard-stack",
        content="The dashboard is a Vite React app.",
    )
    # "registration" appears in NO memory → the AND query is empty; the OR
    # fallback still surfaces the walkthrough memory (the #133 felt failure).
    out = memory.recall(db_session, query="walkthrough plugin usage registration")
    assert out["match_mode"] == "any_term"
    assert out["items_total"] == 1
    assert out["memories"][0]["name"] == "walkthrough-plugin-usage"


def test_recall_multiword_all_terms_stays_strict(db_session):
    """When the strict query DOES match, no fallback happens — precision kept."""
    memory.remember(
        db_session,
        name="postgres-store",
        content="Postgres is the durable store for all Snowline data.",
    )
    memory.remember(
        db_session,
        name="postgres-tuning",
        content="Postgres tuning notes.",
    )
    out = memory.recall(db_session, query="postgres durable store")
    assert out["match_mode"] == "all_terms"
    assert out["items_total"] == 1  # only the row with ALL terms
    assert out["memories"][0]["name"] == "postgres-store"


def test_recall_single_word_miss_does_not_relax(db_session):
    memory.remember(db_session, content="a", name="only-note")
    out = memory.recall(db_session, query="nonexistentterm")
    assert out["items_total"] == 0
    assert out["match_mode"] == "all_terms"


def test_recall_relaxed_ranks_more_hits_first(db_session):
    memory.remember(
        db_session,
        name="two-hits",
        content="alpha beta together here",
    )
    memory.remember(
        db_session,
        name="one-hit",
        content="alpha alone",
    )
    # "gamma" matches nothing → relaxed; two-hits matches 2 of 3 terms and
    # must outrank one-hit.
    out = memory.recall(db_session, query="alpha beta gamma")
    assert out["match_mode"] == "any_term"
    names = [m["name"] for m in out["memories"]]
    assert names == ["two-hits", "one-hit"]


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


def test_forget_normalizes_underscore_name(db_session):
    # Issue #48: a caller who saved `my_note` (stored as `my-note`) must be able
    # to forget it by the name they remembered it under.
    memory.remember(db_session, content="x", name="my_note")
    gone = memory.forget(db_session, "my_note")
    assert gone == {"forgotten": True, "name": "my-note"}
    assert memory.list_memories(db_session)["items_total"] == 0


# --- tombstones + local resurrection (#80) ----------------------------------


def test_forget_keeps_a_tombstone_row(db_session):
    # #80: `forget` no longer HARD-DELETES — it tombstones (so a late-arriving
    # older `set` can't resurrect the memory). The row survives, marked
    # `forgotten`, and is simply excluded from every read.
    from sqlalchemy import select

    from snowline_memory.models import Memory

    memory.remember(db_session, content="x", name="doomed")
    memory.forget(db_session, "doomed")

    row = db_session.scalar(select(Memory).where(Memory.name == "doomed"))
    assert row is not None and row.forgotten is True
    assert memory.list_memories(db_session)["items_total"] == 0
    assert memory.recall(db_session)["items_total"] == 0
    assert memory.memory_digest(db_session)["items_total"] == 0


def test_local_remember_after_forget_resurrects(clean_db):
    # A fresh local `remember` (newer clock) wins the LWW compare over the
    # tombstone and brings the memory back — in place (same row, `created` False).
    # Separate transactions so the authoring clock advances, as in production.
    from snowline_memory.db import session_scope

    with session_scope() as s:
        memory.remember(s, content="v1", name="phoenix")
    with session_scope() as s:
        memory.forget(s, "phoenix")
    with session_scope() as s:
        assert memory.list_memories(s)["items_total"] == 0
    with session_scope() as s:
        again = memory.remember(s, content="v2", name="phoenix")
        assert again["created"] is False  # same register, un-tombstoned
    with session_scope() as s:
        out = memory.recall(s)
        assert out["items_total"] == 1
        assert out["memories"][0]["content"] == "v2"


def test_rapid_remember_then_forget_same_transaction(db_session):
    # The monotonic-clock guard (`_monotonic_local_at`): a `forget` immediately
    # after a `remember` in the SAME transaction must not tie the microsecond and
    # lose — the forget must land.
    memory.remember(db_session, content="x", name="fleeting")
    out = memory.forget(db_session, "fleeting")
    assert out["forgotten"] is True
    assert memory.list_memories(db_session)["items_total"] == 0


# --- issue #47: pin every row/header shape's exact key set -------------------
#
# The plugin's serializers already emit `scope`/`kind` (unlike the old monolith
# this issue was audited against) — these tests exist so a future field drop
# fails loudly instead of quietly regressing scoped rows back to "unscoped".


def test_row_shape_pins_keys(db_session):
    row = memory.remember(
        db_session, content="x", name="pin-row", kind="gotcha", scope="acme/widget"
    )
    assert set(row.keys()) == {
        "id",
        "name",
        "description",
        "content",
        "kind",
        "scope",
        "created_at",
        "updated_at",
        "created",
    }
    assert row["scope"] == "acme/widget"
    assert row["kind"] == "gotcha"


def test_recall_row_shape_pins_keys_with_query(db_session):
    memory.remember(
        db_session,
        content="postgres is the durable store",
        name="pin-recall-fts",
        kind="reference",
        scope="acme/widget",
    )
    out = memory.recall(db_session, query="postgres")
    row = out["memories"][0]
    assert set(row.keys()) == {
        "id",
        "name",
        "description",
        "content",
        "kind",
        "scope",
        "created_at",
        "updated_at",
        "rank",
    }
    assert row["scope"] == "acme/widget"
    assert row["kind"] == "reference"


def test_recall_row_shape_pins_keys_without_query(db_session):
    memory.remember(
        db_session, content="x", name="pin-recall-plain", kind="user", scope="acme/widget"
    )
    out = memory.recall(db_session)
    row = out["memories"][0]
    assert set(row.keys()) == {
        "id",
        "name",
        "description",
        "content",
        "kind",
        "scope",
        "created_at",
        "updated_at",
    }
    assert row["scope"] == "acme/widget"
    assert row["kind"] == "user"


def test_header_shape_pins_keys(db_session):
    memory.remember(
        db_session, content="x", name="pin-header", kind="feedback", scope="acme/widget"
    )
    out = memory.list_memories(db_session)
    hdr = out["memories"][0]
    assert set(hdr.keys()) == {"name", "description", "kind", "scope", "updated_at"}
    assert hdr["scope"] == "acme/widget"
    assert hdr["kind"] == "feedback"


def test_digest_entry_shape_pins_keys(db_session):
    memory.remember(
        db_session, content="x", name="pin-digest", kind="user", scope="acme/widget"
    )
    out = memory.memory_digest(db_session)
    entry = out["groups"][0]["entries"][0]
    # `kind` is the group key, not a per-entry field — the entry shape is
    # deliberately name/description/scope.
    assert set(entry.keys()) == {"name", "description", "scope"}
    assert entry["scope"] == "acme/widget"
    assert out["groups"][0]["kind"] == "user"
