"""Test harness: a disposable Postgres database, schema built via Alembic.

Mirrors the frozen monolith's conftest. The test database is created from
scratch, migrated with `alembic upgrade head` (so the migration chain is
exercised by every run), and each test gets a clean `scopes` table via TRUNCATE.

If Postgres is unreachable (no local server / can't create the DB), the
DB-backed fixtures `pytest.skip` with a clear message rather than erroring — the
import-purity and route-shape tests that don't need the DB still run.
"""

import os
from pathlib import Path

import pytest

# Point the platform's DB layer at the disposable test database BEFORE any
# platform module builds its (lazy) engine.
TEST_DB_URL = os.environ.get(
    "SNOWLINE_PLATFORM_TEST_DATABASE_URL",
    "postgresql+psycopg:///snowline_platform_test",
)
os.environ["SNOWLINE_PLATFORM_DATABASE_URL"] = TEST_DB_URL

import sqlalchemy as sa  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

MIGRATIONS = (
    Path(__file__).parents[1]
    / "src"
    / "snowline_platform"
    / "migrations"
)


def _db_name(url: str) -> str:
    return sa.make_url(url).database


def _maintenance_url(url: str) -> str:
    # render_as_string, NOT str(): str() masks the password as `***`, which the
    # maintenance engine would then send literally — a password-bearing test DB
    # URL could never connect.
    return sa.make_url(url).set(database="postgres").render_as_string(
        hide_password=False
    )


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", os.environ["SNOWLINE_PLATFORM_DATABASE_URL"])
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
    """A freshly created + migrated test database for the whole session."""
    if not _postgres_reachable():
        pytest.skip(
            "Postgres not reachable at "
            f"{_maintenance_url(TEST_DB_URL)!r} — DB-backed tests skipped"
        )
    from snowline_platform.db import reset_engine

    drop_database(TEST_DB_URL)
    create_database(TEST_DB_URL)
    reset_engine()
    command.upgrade(alembic_config(), "head")
    yield TEST_DB_URL
    reset_engine()
    drop_database(TEST_DB_URL)


@pytest.fixture()
def clean_db(migrated_db):
    """Truncate the scopes table AND the adopted replication tables (spec §8,
    issue #81) before each test (so writes a test makes are visible across the
    separate sessions the service/route open — mirroring production). CASCADE
    covers `replication_outbox`'s FK to `replication_subscriptions`."""
    from snowline_platform.db import session_scope

    with session_scope() as s:
        s.execute(
            sa.text(
                "TRUNCATE scopes, milestones, milestone_transitions, "
                "milestone_dependencies, replication_subscriptions, "
                "replication_outbox, replication_stream_counters, "
                "replication_inbound_streams, replication_parked_events "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture()
def db_session(clean_db):
    """A clean ORM session per test (auto-commit on success)."""
    from snowline_platform.db import session_scope

    with session_scope() as s:
        yield s
