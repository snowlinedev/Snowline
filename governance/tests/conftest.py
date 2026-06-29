"""Governance test harness: a disposable Postgres DB, schema built via Alembic.

Mirrors the platform's `tests/conftest.py`. The governance test database is
created from scratch, migrated with `alembic upgrade head` (so the migration
chain is exercised every run), and each test gets a clean `decisions` table via
TRUNCATE.

If Postgres is unreachable, the DB-backed fixtures `pytest.skip` with a clear
message rather than erroring — the import-purity / registration / stub-based
tests that don't need the DB still run.
"""

import os
import uuid
from pathlib import Path

import pytest

# Point governance's DB layer at the disposable test database BEFORE any
# governance module builds its (lazy) engine.
TEST_DB_URL = os.environ.get(
    "SNOWLINE_GOVERNANCE_TEST_DATABASE_URL",
    "postgresql+psycopg:///snowline_governance_test",
)
os.environ["SNOWLINE_GOVERNANCE_DATABASE_URL"] = TEST_DB_URL

import sqlalchemy as sa  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

MIGRATIONS = (
    Path(__file__).parents[1] / "src" / "snowline_governance" / "migrations"
)


def _db_name(url: str) -> str:
    return sa.make_url(url).database


def _maintenance_url(url: str) -> str:
    return str(sa.make_url(url).set(database="postgres"))


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option(
        "sqlalchemy.url", os.environ["SNOWLINE_GOVERNANCE_DATABASE_URL"]
    )
    return cfg


def _postgres_reachable() -> bool:
    try:
        eng = sa.create_engine(
            _maintenance_url(TEST_DB_URL), isolation_level="AUTOCOMMIT"
        )
        with eng.connect():
            pass
        eng.dispose()
        return True
    except Exception:
        return False


def create_database(url: str) -> None:
    name = _db_name(url)
    eng = sa.create_engine(_maintenance_url(url), isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        exists = conn.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        ).scalar()
        if not exists:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    eng.dispose()


def drop_database(url: str) -> None:
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
    eng.dispose()


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """A freshly created + migrated governance test database for the session."""
    if not _postgres_reachable():
        pytest.skip(
            "Postgres not reachable at "
            f"{_maintenance_url(TEST_DB_URL)!r} — DB-backed tests skipped"
        )
    from snowline_governance.db import reset_engine

    drop_database(TEST_DB_URL)
    create_database(TEST_DB_URL)
    reset_engine()
    command.upgrade(alembic_config(), "head")
    yield TEST_DB_URL
    reset_engine()
    drop_database(TEST_DB_URL)


@pytest.fixture()
def clean_db(migrated_db):
    """Truncate the governance tables before each test (so writes a test makes are
    visible across the separate sessions a tool opens — mirroring production).
    Lists every root table explicitly; CASCADE clears the dependent version/govern
    rows."""
    import sqlalchemy as sa

    from snowline_governance.db import session_scope

    with session_scope() as s:
        s.execute(
            sa.text(
                "TRUNCATE decisions, artifacts, artifact_versions, "
                "artifact_governs, shadow_branches, shadow_nodes, "
                "shadow_node_citations, shadow_conversation_events, "
                "webhook_subscriptions, webhook_deliveries "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture()
def db_session(clean_db):
    """A clean ORM session per test (auto-commit on success)."""
    from snowline_governance.db import session_scope

    with session_scope() as s:
        yield s


# --- a stub ScopeClient so unit tests need no running platform --------------


class StubScopeClient:
    """An in-memory `ScopeClient` double (governance-plugin spec: applicability
    must be testable without a live platform).

    `tree` maps slug -> parent slug (None for a root). `isolated` is the set of
    slugs that block inheritance from above. It computes `ancestors(slug)` with
    the SAME isolation-halting rule the platform's scope service implements (own
    scope first, climb to parent, stop at the first `isolated` node + the root)
    and records every slug it was asked about — so a test can assert governance
    queried the chain and merged it. `_row` matches the platform's `to_row`
    shape (incl. `id`, the soft reference governance stores).
    """

    def __init__(
        self,
        tree: dict[str, str | None],
        isolated: set[str] | None = None,
    ) -> None:
        self._tree = tree
        self._isolated = isolated or set()
        self._ids = {
            slug: uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}") for slug in tree
        }
        self.ancestors_calls: list[str] = []
        self.resolve_calls: list[str] = []

    def _row(self, slug: str) -> dict:
        return {
            "id": str(self._ids[slug]),
            "slug": slug,
            "name": slug,
            "kind": "org" if "/" not in slug else "project",
            "status": "active",
            "isolated": slug in self._isolated,
            "org": slug.split("/", 1)[0],
        }

    def resolve(self, slug: str) -> dict | None:
        self.resolve_calls.append(slug)
        return self._row(slug) if slug in self._tree else None

    def ancestors(self, slug: str) -> list[dict]:
        self.ancestors_calls.append(slug)
        chain: list[dict] = []
        seen: set[str] = set()
        node: str | None = slug
        while node is not None and node in self._tree and node not in seen:
            seen.add(node)
            chain.append(self._row(node))
            if node in self._isolated:  # blocks inheritance from ABOVE
                break
            node = self._tree[node]
        return chain


@pytest.fixture()
def stub_scope_client():
    """Factory: `stub_scope_client(tree, isolated=...)` -> `StubScopeClient`."""
    return StubScopeClient
