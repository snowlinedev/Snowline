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
# Importing `snowline_platform.app` builds the production singleton, which
# persists the registered-plugin set (issue #240) — disable that file so a test
# run never writes into the developer's real per-machine state. Tests that
# exercise it pass an explicit tmp path to `create_app`.
os.environ["SNOWLINE_PLATFORM_STATE_FILE"] = ""

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
                "milestone_dependencies, milestone_unreconciled, "
                "replication_subscriptions, "
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


# --- two-instance harness (§7.1 union, #98) ----------------------------------
#
# The cold-start union needs TWO complete platform stores (scopes + the SDK
# replication tables), each migrated via the alembic chain, with independent
# engines — the same shape as memory's `repl_a`/`repl_b` convergence harness.
# Names DERIVE from the test database's own name, so a session pointed at a
# throwaway SNOWLINE_PLATFORM_TEST_DATABASE_URL stays isolated.

_UNION_DB_URLS = {
    name: sa.make_url(TEST_DB_URL)
    .set(database=f"{_db_name(TEST_DB_URL)}_{name}")
    .render_as_string(hide_password=False)
    for name in ("union_a", "union_b")
}

_UNION_TABLES = (
    "scopes, milestones, milestone_transitions, milestone_dependencies, "
    "milestone_unreconciled, replication_subscriptions, replication_outbox, "
    "replication_stream_counters, replication_inbound_streams, "
    "replication_parked_events"
)


@pytest.fixture(scope="session")
def _platform_union_stores() -> dict:
    if not _postgres_reachable():
        pytest.skip("Postgres not reachable — platform union tests skipped")
    from sqlalchemy.orm import sessionmaker

    makers = {}
    prev = os.environ.get("SNOWLINE_PLATFORM_DATABASE_URL")
    try:
        for name, url in _UNION_DB_URLS.items():
            drop_database(url)
            create_database(url)
            # env.py reads the URL from SNOWLINE_PLATFORM_DATABASE_URL.
            os.environ["SNOWLINE_PLATFORM_DATABASE_URL"] = url
            command.upgrade(alembic_config(), "head")
            engine = sa.create_engine(url, future=True)
            makers[name] = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    finally:
        if prev is not None:
            os.environ["SNOWLINE_PLATFORM_DATABASE_URL"] = prev
    yield makers
    for maker in makers.values():
        maker.kw["bind"].dispose()
    for url in _UNION_DB_URLS.values():
        drop_database(url)


@pytest.fixture()
def platform_stores(_platform_union_stores) -> dict:
    """Both union stores truncated clean before each test."""
    for maker in _platform_union_stores.values():
        with maker() as s:
            s.execute(sa.text(f"TRUNCATE {_UNION_TABLES} RESTART IDENTITY CASCADE"))
            s.commit()
    return _platform_union_stores


@pytest.fixture(autouse=True)
def _reset_replication_delivery_health():
    """The SDK's delivery-health state (issue #241) is process-wide: a test
    that drives `deliver_pending` into failures must not leave a stale
    `unreachable_since` that turns a later test's `/health` degraded."""
    from snowline_plugin_sdk.replication.health import DELIVERY_HEALTH

    DELIVERY_HEALTH.reset()
    yield
    DELIVERY_HEALTH.reset()


@pytest.fixture(autouse=True)
def _isolate_system_launch_daemons(tmp_path, monkeypatch):
    """The source-run-hub guard also looks in /Library/LaunchDaemons (#239);
    never let a real hub's installed daemons leak into sync tests."""
    from snowline_platform.stack import model as stack_model

    monkeypatch.setattr(stack_model, "SYSTEM_LAUNCH_DAEMONS_DIR", tmp_path / "_no_system_daemons")
