"""Memory test harness: a disposable Postgres DB, schema built via Alembic.

Mirrors governance's `tests/conftest.py`. The memory test database is created
from scratch, migrated with `alembic upgrade head` (so the migration chain — and
the generated tsvector column + GIN index — is exercised every run), and each
test gets a clean `memories` table via TRUNCATE.

If Postgres is unreachable, the DB-backed fixtures `pytest.skip` with a clear
message rather than erroring — the import-purity / registration tests that don't
need the DB still run.
"""

import os
from pathlib import Path

import pytest

# Point memory's DB layer at the disposable test database BEFORE any memory module
# builds its (lazy) engine.
TEST_DB_URL = os.environ.get(
    "SNOWLINE_MEMORY_TEST_DATABASE_URL",
    "postgresql+psycopg:///snowline_memory_test",
)
os.environ["SNOWLINE_MEMORY_DATABASE_URL"] = TEST_DB_URL

import sqlalchemy as sa  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

MIGRATIONS = Path(__file__).parents[1] / "src" / "snowline_memory" / "migrations"


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
    cfg.set_main_option("sqlalchemy.url", os.environ["SNOWLINE_MEMORY_DATABASE_URL"])
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
    """A freshly created + migrated memory test database for the session."""
    if not _postgres_reachable():
        pytest.skip(
            "Postgres not reachable at "
            f"{_maintenance_url(TEST_DB_URL)!r} — DB-backed tests skipped"
        )
    from snowline_memory.db import reset_engine

    drop_database(TEST_DB_URL)
    create_database(TEST_DB_URL)
    reset_engine()
    command.upgrade(alembic_config(), "head")
    yield TEST_DB_URL
    reset_engine()
    drop_database(TEST_DB_URL)


@pytest.fixture()
def clean_db(migrated_db):
    """Truncate the memories table before each test (so writes a test makes are
    visible across the separate sessions a tool opens — mirroring production)."""
    from snowline_memory.db import session_scope

    with session_scope() as s:
        s.execute(sa.text("TRUNCATE memories RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture()
def db_session(clean_db):
    """A clean ORM session per test (auto-commit on success)."""
    from snowline_memory.db import session_scope

    with session_scope() as s:
        yield s


# --- two-instance replication harness (#80) ---------------------------------
#
# The §10 memory-convergence criteria need TWO complete memory stores (a hub and
# a spoke), each with the domain `memories` table AND the SDK replication tables,
# wired sender→receiver. Two disposable databases, both migrated via the alembic
# chain (so the replication-tables migration is exercised), each with its own
# independent engine/sessionmaker — the memory `db.py` global is left untouched.

from sqlalchemy.orm import sessionmaker  # noqa: E402

_REPL_DB_URLS = {
    # render_as_string(hide_password=False), not str(): these URLs are used
    # directly to create the real replication-store engines below, so a masked
    # `***` password would break connections the same way it broke the
    # maintenance URL.
    name: sa.make_url(TEST_DB_URL)
    .set(database=f"snowline_memory_{name}")
    .render_as_string(hide_password=False)
    for name in ("repl_a", "repl_b")
}


@pytest.fixture(scope="session")
def _replication_stores() -> dict:
    """Two freshly created + migrated memory databases (`repl_a`, `repl_b`), each
    with an independent sessionmaker — the hub and spoke of the convergence
    tests."""
    if not _postgres_reachable():
        pytest.skip("Postgres not reachable — replication convergence tests skipped")
    makers = {}
    prev = os.environ.get("SNOWLINE_MEMORY_DATABASE_URL")
    for name, url in _REPL_DB_URLS.items():
        drop_database(url)
        create_database(url)
        # env.py resets the alembic url from SNOWLINE_MEMORY_DATABASE_URL, so
        # point it at THIS store for the migration; each instance then gets its
        # own engine bound to `url` directly.
        os.environ["SNOWLINE_MEMORY_DATABASE_URL"] = url
        command.upgrade(alembic_config(), "head")
        engine = sa.create_engine(url, future=True)
        makers[name] = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    if prev is not None:
        os.environ["SNOWLINE_MEMORY_DATABASE_URL"] = prev
    yield makers
    for maker in makers.values():
        maker.kw["bind"].dispose()
    for url in _REPL_DB_URLS.values():
        drop_database(url)


@pytest.fixture()
def memory_stores(_replication_stores) -> dict:
    """Both stores truncated clean before each test — every replication table and
    the memories table (so a test's rows never leak into the next)."""
    tables = (
        "memories",
        "replication_subscriptions",
        "replication_outbox",
        "replication_stream_counters",
        "replication_inbound_streams",
        "replication_parked_events",
    )
    for maker in _replication_stores.values():
        with maker() as s:
            for t in tables:
                s.execute(sa.text(f"TRUNCATE {t} RESTART IDENTITY CASCADE"))
            s.commit()
    return _replication_stores
