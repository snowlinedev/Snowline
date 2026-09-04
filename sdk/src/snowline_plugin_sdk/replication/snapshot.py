"""Service-side database snapshots — the §7 seeding dump, produced BY the
service that owns the store (item 0ebe6a70 / snowlinedev/Snowline#221).

Governance decision **1a83031c** (2026-09-04): the hub's Postgres stays
loopback-only and is never exposed on the tailnet, not even behind password
auth. All cross-instance data movement goes through a Snowline service's own
HTTP surface behind the trust gate. Seeding used to run `pg_dump` FROM the
spoke against a `postgresql://user@<hub-tailnet>:5432/<db>` URL; that is now
forbidden, so the PRIMARY dumps its OWN database (over its loopback/socket
connection, the one it already holds) and streams the archive back over the
replication-admin surface (`admin.build_replication_router`'s
`POST {admin_prefix}/snapshot`).

This module is the shared half of that move: URL normalization + password
hygiene used by BOTH sides — the service running `pg_dump` here, and the
seed client running `pg_restore` there (`snowline_platform.replication_seed`
imports these rather than keeping a second copy).

Password hygiene, non-negotiable on either side: a password NEVER rides
`pg_dump`/`pg_restore` argv (`ps` is world-readable on a shared box) — it is
lifted out of the URL into a `PGPASSWORD` env overlay, and the scrubbed URL is
what appears in argv, logs, and error text.

Stdlib + sqlalchemy only (no fastapi): `admin` imports this, not the reverse,
so a plugin wiring its own transport can still reuse it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

# The custom-format archive flags §7 step 2 depends on: `-Fc` so the restore
# side can use `pg_restore --clean --if-exists` (a plain SQL dump cannot),
# `--no-owner --no-privileges` because the two instances are different owner
# Macs with different role names.
PG_DUMP_FLAGS = ("-Fc", "--no-owner", "--no-privileges")


class SnapshotError(RuntimeError):
    """`pg_dump` failed. The caller must NOT serve or restore a partial
    archive — a partially-restored store is §7's silent-data-loss mode."""


def libpq_url(url: str) -> str:
    """A libpq-consumable URL for pg_dump/pg_restore: strip SQLAlchemy's
    `+psycopg` (or any `+driver`) so the CLI tools see a plain `postgresql://`
    scheme."""
    if url.startswith("postgresql+"):
        return "postgresql://" + url.split("://", 1)[1]
    if url.startswith("postgres+"):
        return "postgres://" + url.split("://", 1)[1]
    return url


def libpq_url_and_env(url) -> tuple[str, dict[str, str]]:
    """A libpq URL with the password LIFTED OUT of the URL and into a
    `PGPASSWORD` env fragment, so it never rides pg_dump/pg_restore's argv
    (visible in `ps`). Returns `(url_without_password, env_overlay)`; the
    overlay is empty when the URL carries no password (peer/trust auth, or a
    socket connection).

    Accepts either a URL STRING (the seed's configured `spoke_db_url`) or a
    SQLAlchemy `URL` object (a service's own `engine.url`, via
    `database_url()`) — one implementation for both sides of the snapshot.
    """
    if hasattr(url, "render_as_string"):  # a sqlalchemy.engine.URL
        return _from_sqlalchemy_url(url)
    plain = libpq_url(str(url))
    parts = urlsplit(plain)
    if not parts.password:
        return plain, {}
    userinfo = parts.username or ""
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{userinfo}@{host}" if userinfo else host
    scrubbed = urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )
    # urlsplit hands back the still-percent-encoded password; libpq wants the
    # decoded value in PGPASSWORD.
    return scrubbed, {"PGPASSWORD": unquote(parts.password)}


def _from_sqlalchemy_url(url) -> tuple[str, dict[str, str]]:
    """The `URL`-object path: rebuild the URL through `URL.create` WITHOUT the
    password (exact escaping, no string surgery) and hand the raw password
    back as the env overlay."""
    from sqlalchemy.engine import URL

    scrubbed = URL.create(
        drivername=url.drivername.split("+", 1)[0],
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    )
    env = {"PGPASSWORD": str(url.password)} if url.password else {}
    return scrubbed.render_as_string(hide_password=False), env


def database_url(session):
    """The URL of the database THIS session is bound to — the service's own
    store, which is exactly what the snapshot route dumps. Read off the
    session's engine bind so mounting the router needs no extra argument (the
    plugin already handed the SDK a bound `session_scope`)."""
    bind = session.get_bind()
    url = getattr(bind, "url", None)
    if url is None:  # pragma: no cover - a connection-bound session
        raise SnapshotError(
            "the replication session is not bound to an engine, so its "
            "database URL cannot be resolved for pg_dump"
        )
    return url


def pg_dump_argv(url: str, dest: str | Path) -> list[str]:
    """The exact `pg_dump` argv. The URL here is already password-scrubbed
    (see `libpq_url_and_env`) — nothing secret is ever in this list."""
    return ["pg_dump", *PG_DUMP_FLAGS, "-f", str(dest), url]


def run_pg_dump(url, dest: str | Path) -> None:
    """Dump the database at `url` (a string or a SQLAlchemy `URL`) into `dest`
    as a custom-format archive. ANY non-zero exit raises `SnapshotError` — a
    truncated archive must never be streamed to a seeding spoke."""
    scrubbed, env = libpq_url_and_env(url)
    proc = subprocess.run(
        pg_dump_argv(scrubbed, dest),
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
    )
    if proc.returncode != 0:
        # stderr can name the scrubbed URL; the password is only ever in env.
        raise SnapshotError(
            f"pg_dump failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
