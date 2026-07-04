"""SDK test harness for the replication modules (issue #77).

Unlike the plugins' suites (a disposable Postgres migrated via their alembic
chains), the SDK owns no migration chain and must stay testable STANDALONE — so
the replication tests run on in-memory SQLite via `ReplicationBase.metadata.
create_all` (the models use a JSON/JSONB variant precisely so this works).
`StaticPool` pins the one in-memory database to every connection the
sessionmaker opens; `expire_on_commit=False` matches the plugins' sessionmakers
(`deliver_pending` commits per row and the tests keep reading the same
objects).

Each `session`/`make_instance` is a FRESH database — no cross-test truncation
needed. `make_instance` builds an independent (engine, sessionmaker) pair so
the round-trip tests can stand up TWO complete "instances" (hub + spoke) in one
process.

The replication env contract: `SNOWLINE_REPLICATION_SOURCE_ID` is fail-loud
(spec §3 — a defaulted source id would fork stream identity), so an autouse
fixture pins a test identity; individual tests override via monkeypatch.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from snowline_plugin_sdk.replication.models import ReplicationBase


@pytest.fixture(autouse=True)
def _replication_source_id(monkeypatch):
    """A pinned test source id (`SNOWLINE_REPLICATION_SOURCE_ID` is fail-loud)."""
    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "test.plugin")


def _make_sessionmaker() -> sessionmaker:
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    ReplicationBase.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture()
def make_instance():
    """Factory: each call is an independent in-memory replication store (its own
    engine + sessionmaker) — one per simulated instance."""
    return _make_sessionmaker


@pytest.fixture()
def session(make_instance):
    """One session over a fresh store (commit-friendly; closed at teardown)."""
    s = make_instance()()
    try:
        yield s
    finally:
        s.close()
