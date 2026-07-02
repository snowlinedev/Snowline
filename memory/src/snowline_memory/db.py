"""Memory's DB layer — engine, sessionmaker, and `session_scope()`.

Memory has its OWN database (memory-plugin spec §2, separate from the platform's
and governance's). Mirrors governance's `db.py`: the engine/sessionmaker are
built lazily on first use, not at import time, so the database URL is read when a
session is actually opened — which lets tests point at a disposable database and
avoids connecting just by importing the package.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from snowline_memory.config import database_url

_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(database_url(), future=True)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _sessionmaker


def reset_engine() -> None:
    """Drop the cached engine/sessionmaker (used by tests after switching URL)."""
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
