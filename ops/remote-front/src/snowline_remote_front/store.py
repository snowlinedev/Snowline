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

import sqlite3
import threading
from typing import Protocol

from mcp.server.auth.provider import AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull


class Store(Protocol):
    def get_client(self, client_id: str) -> OAuthClientInformationFull | None: ...
    def put_client(self, client: OAuthClientInformationFull) -> None: ...

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
        raw = self._clients.get(client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    def put_client(self, client: OAuthClientInformationFull) -> None:
        assert client.client_id is not None
        self._clients[client.client_id] = client.model_dump_json()

    def get_auth_code(self, code: str) -> AuthorizationCode | None:
        raw = self._codes.get(code)
        return AuthorizationCode.model_validate_json(raw) if raw else None

    def put_auth_code(self, auth_code: AuthorizationCode) -> None:
        self._codes[auth_code.code] = auth_code.model_dump_json()

    def delete_auth_code(self, code: str) -> None:
        self._codes.pop(code, None)

    def get_refresh_token(self, token: str) -> RefreshToken | None:
        raw = self._refresh.get(token)
        return RefreshToken.model_validate_json(raw) if raw else None

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
        raw = self._get("clients", client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    def put_client(self, client: OAuthClientInformationFull) -> None:
        assert client.client_id is not None
        self._put("clients", client.client_id, client.model_dump_json())

    def get_auth_code(self, code: str) -> AuthorizationCode | None:
        raw = self._get("auth_codes", code)
        return AuthorizationCode.model_validate_json(raw) if raw else None

    def put_auth_code(self, auth_code: AuthorizationCode) -> None:
        self._put("auth_codes", auth_code.code, auth_code.model_dump_json())

    def delete_auth_code(self, code: str) -> None:
        self._delete("auth_codes", code)

    def get_refresh_token(self, token: str) -> RefreshToken | None:
        raw = self._get("refresh_tokens", token)
        return RefreshToken.model_validate_json(raw) if raw else None

    def put_refresh_token(self, refresh_token: RefreshToken) -> None:
        self._put("refresh_tokens", refresh_token.token, refresh_token.model_dump_json())

    def delete_refresh_token(self, token: str) -> None:
        self._delete("refresh_tokens", token)
