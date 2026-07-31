"""The persistent state the front keeps: DCR client registrations, authorization
codes (short-lived), and refresh tokens. NO Snowline domain data ever lives here
(issue #120 non-goal) — only OAuth bookkeeping.

Two implementations behind one `Store` protocol:

  - `InMemoryStore` — the default and the test store. Restart loses everything;
    that's the "lose nothing but live sessions" degradation when no volume is
    attached.
  - `SqliteStore` — a single tiny SQLite file (a fly volume in deploy). Client
    registrations + refresh tokens persist, so restarting/redeploying the app
    never forces re-adding the Claude.ai connector (issue #120 acceptance).

Records are stored as their pydantic JSON, so the store is a dumb key/value blob
per kind and the OAuth models stay the source of truth.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Protocol, TypeVar

from mcp.server.auth.provider import AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import BaseModel, ValidationError

log = logging.getLogger("snowline_remote_front.store")

_M = TypeVar("_M", bound=BaseModel)


def _load(model: type[_M], raw: str | None, kind: str) -> _M | None:
    """Decode one stored record, degrading a CORRUPT/schema-drifted blob to
    "absent" (with a WARNING) instead of raising.

    A record written by an older model version — or a truncated write — would
    otherwise raise `ValidationError` out of every read that touches it: an
    unreadable client registration would 500 the whole OAuth flow rather than
    letting the client re-register, and an unreadable refresh token would 500
    the refresh instead of 401-ing the client into a fresh authorization. The
    corruption is never silent: it is logged, and the caller's None path is the
    same recoverable "unknown record" branch it already handles."""
    if not raw:
        return None
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        log.warning(
            "remote-front: discarding unreadable stored %s record: %s", kind, exc
        )
        return None


class Store(Protocol):
    def get_client(self, client_id: str) -> OAuthClientInformationFull | None: ...
    def put_client(self, client: OAuthClientInformationFull) -> None: ...
    def prune_clients(self, max_clients: int) -> bool: ...

    def get_auth_code(self, code: str) -> AuthorizationCode | None: ...
    def put_auth_code(self, auth_code: AuthorizationCode) -> None: ...
    def delete_auth_code(self, code: str) -> None: ...

    def get_refresh_token(self, token: str) -> RefreshToken | None: ...
    def put_refresh_token(self, refresh_token: RefreshToken) -> None: ...
    def delete_refresh_token(self, token: str) -> None: ...


class InMemoryStore:
    """Dict-backed store — the default and the test store."""

    def __init__(self) -> None:
        self._clients: dict[str, str] = {}
        self._codes: dict[str, str] = {}
        self._refresh: dict[str, str] = {}

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return _load(OAuthClientInformationFull, self._clients.get(client_id), "client")

    def put_client(self, client: OAuthClientInformationFull) -> None:
        assert client.client_id is not None
        self._clients[client.client_id] = client.model_dump_json()

    def prune_clients(self, max_clients: int) -> bool:
        """Make room for one more registration under `max_clients` (the open-DCR
        disk/memory-fill guard): evict oldest-first (dict insertion order), but
        ONLY clients with no live refresh token — a live connector is never
        broken by a registration flood. Returns True if there is room."""
        if len(self._clients) < max_clients:
            return True
        # An unreadable refresh-token row can't vouch for a client, but it must
        # not break the prune either (`_load` logs and returns None).
        active = {
            token.client_id
            for token in (
                _load(RefreshToken, raw, "refresh token")
                for raw in self._refresh.values()
            )
            if token is not None
        }
        for client_id in list(self._clients):
            if len(self._clients) < max_clients:
                break
            if client_id not in active:
                del self._clients[client_id]
        return len(self._clients) < max_clients

    def get_auth_code(self, code: str) -> AuthorizationCode | None:
        return _load(AuthorizationCode, self._codes.get(code), "authorization code")

    def put_auth_code(self, auth_code: AuthorizationCode) -> None:
        self._codes[auth_code.code] = auth_code.model_dump_json()

    def delete_auth_code(self, code: str) -> None:
        self._codes.pop(code, None)

    def get_refresh_token(self, token: str) -> RefreshToken | None:
        return _load(RefreshToken, self._refresh.get(token), "refresh token")

    def put_refresh_token(self, refresh_token: RefreshToken) -> None:
        self._refresh[refresh_token.token] = refresh_token.model_dump_json()

    def delete_refresh_token(self, token: str) -> None:
        self._refresh.pop(token, None)


class SqliteStore:
    """SQLite-file store for deploy (a fly volume keeps it across restarts).

    One connection guarded by a lock: the front's write volume is tiny (a
    handful of clients + refresh tokens), the calls are sub-millisecond, and
    serializing them keeps the sync SQLite calls safe under the async server
    without an async DB dependency."""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        for table in ("clients", "auth_codes", "refresh_tokens"):
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, json TEXT NOT NULL)"
            )
        self._conn.commit()

    def _get(self, table: str, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT json FROM {table} WHERE id = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def _put(self, table: str, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {table} (id, json) VALUES (?, ?) "
                f"ON CONFLICT(id) DO UPDATE SET json = excluded.json",
                (key, value),
            )
            self._conn.commit()

    def _delete(self, table: str, key: str) -> None:
        with self._lock:
            self._conn.execute(f"DELETE FROM {table} WHERE id = ?", (key,))
            self._conn.commit()

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return _load(
            OAuthClientInformationFull, self._get("clients", client_id), "client"
        )

    def put_client(self, client: OAuthClientInformationFull) -> None:
        assert client.client_id is not None
        self._put("clients", client.client_id, client.model_dump_json())

    def prune_clients(self, max_clients: int) -> bool:
        """Same contract as `InMemoryStore.prune_clients` — oldest-first
        (rowid order) eviction of token-less clients only. json_extract is
        SQLite's built-in JSON1 (bundled with CPython's sqlite3)."""
        with self._lock:
            (count,) = self._conn.execute("SELECT COUNT(*) FROM clients").fetchone()
            if count < max_clients:
                return True
            # `json_valid` guard: json_extract raises OperationalError
            # ("malformed JSON") on a corrupt row, which would turn one bad
            # blob into a failure of every subsequent registration. An
            # unreadable row simply vouches for no client (logged below).
            rows = self._conn.execute(
                "SELECT DISTINCT json_extract(json, '$.client_id') "
                "FROM refresh_tokens WHERE json_valid(json)"
            ).fetchall()
            (skipped,) = self._conn.execute(
                "SELECT COUNT(*) FROM refresh_tokens WHERE NOT json_valid(json)"
            ).fetchone()
            active = {row[0] for row in rows if row[0] is not None}
            if skipped:
                log.warning(
                    "remote-front: %d unreadable refresh-token row(s) ignored "
                    "while pruning clients",
                    skipped,
                )
            for (client_id,) in self._conn.execute(
                "SELECT id FROM clients ORDER BY rowid"
            ).fetchall():
                if count < max_clients:
                    break
                if client_id not in active:
                    self._conn.execute(
                        "DELETE FROM clients WHERE id = ?", (client_id,)
                    )
                    count -= 1
            self._conn.commit()
            return count < max_clients

    def get_auth_code(self, code: str) -> AuthorizationCode | None:
        return _load(
            AuthorizationCode, self._get("auth_codes", code), "authorization code"
        )

    def put_auth_code(self, auth_code: AuthorizationCode) -> None:
        self._put("auth_codes", auth_code.code, auth_code.model_dump_json())

    def delete_auth_code(self, code: str) -> None:
        self._delete("auth_codes", code)

    def get_refresh_token(self, token: str) -> RefreshToken | None:
        return _load(
            RefreshToken, self._get("refresh_tokens", token), "refresh token"
        )

    def put_refresh_token(self, refresh_token: RefreshToken) -> None:
        self._put("refresh_tokens", refresh_token.token, refresh_token.model_dump_json())

    def delete_refresh_token(self, token: str) -> None:
        self._delete("refresh_tokens", token)
