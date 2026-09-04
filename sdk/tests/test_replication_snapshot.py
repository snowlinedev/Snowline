"""`POST {admin_prefix}/snapshot` — the §7 step-2 seed dump, served by the
service that OWNS the store (item 0ebe6a70 / snowlinedev/Snowline#221).

Governance decision **1a83031c**: the hub's Postgres stays loopback-only and is
never exposed on the tailnet, not even with password auth; all cross-instance
data movement goes through a Snowline service's own HTTP surface behind the
trust gate. This route is that surface for seeding, so its authorization is the
load-bearing part these tests pin:

  * the trusted-CIDR gate still runs first, but is NOT sufficient (it grants
    owner to EVERY tailnet peer — exactly the exposure the decision rejects);
  * the request must be HMAC-signed under the secret of an ACTIVE OUTBOUND
    subscription toward the caller — i.e. §7 step 1's primed stream, which makes
    prime-first/dump-second mechanical: **no prime, no snapshot**;
  * every refusal answers a byte-identical 404 (non-enumeration): an
    unauthenticated caller cannot distinguish "no such stream" from "bad
    signature" from "route not mounted".

`pg_dump` is faked at the `subprocess` seam — no Postgres needed — which also
lets the argv/env hygiene be asserted directly (password rides PGPASSWORD, never
argv).
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import anyio
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.engine import make_url

from snowline_plugin_sdk.replication import emit as emit_mod
from snowline_plugin_sdk.replication import snapshot as snapshot_mod
from snowline_plugin_sdk.replication.admin import build_replication_router
from snowline_plugin_sdk.replication.envelope import sign_body

TAILNET_PEER = "100.64.0.7"
HOTEL_LAN_PEER = "203.0.113.9"
SNAPSHOT_PATH = "/replication-admin/snapshot"

SOURCE_ID = "primary.governance"
PEER_SOURCE_ID = "roam.governance"
EPOCH = "epoch-2026-09-04"
ARCHIVE = b"PGDMP\x00fake-custom-format-archive"


@pytest.fixture()
def app_and_sessions(make_instance):
    sessions = make_instance()

    @contextmanager
    def session_scope():
        session = sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app = FastAPI()
    app.include_router(build_replication_router(session_scope, lambda s, e: None))
    return app, sessions


def _request(app, method: str, path: str, *, peer=TAILNET_PEER, **kwargs):
    result = {}

    async def main():
        transport = httpx.ASGITransport(app=app, client=(peer, 4242))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://plugin"
        ) as client:
            result["response"] = await client.request(method, path, **kwargs)

    anyio.run(main)
    return result["response"]


def _prime(sessions, *, secret="s3cret-stream-key", peer_source_id=PEER_SOURCE_ID,
           epoch=EPOCH, active=True):
    """The primary's side of §7 step 1: an outbound subscription toward the
    not-yet-booted spoke."""
    session = sessions()
    try:
        sub = emit_mod.create_outbound_subscription(
            session,
            "http://roam-gov/events/ingest",
            secret,
            ["decision.recorded"],
            epoch=epoch,
            source_id=SOURCE_ID,
            peer_source_id=peer_source_id,
        )
        if not active:
            emit_mod.retire_outbound_subscription(session, sub["id"])
        session.commit()
        return sub
    finally:
        session.close()


def _body(source_id=SOURCE_ID, epoch=EPOCH, peer_source_id=PEER_SOURCE_ID) -> bytes:
    return json.dumps(
        {"source_id": source_id, "epoch": epoch, "peer_source_id": peer_source_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _signed(app, body: bytes, secret: str, *, peer=TAILNET_PEER):
    return _request(
        app, "POST", SNAPSHOT_PATH, peer=peer, content=body,
        headers={"X-Snowline-Signature": sign_body(secret, body)},
    )


@pytest.fixture()
def fake_pg_dump(monkeypatch):
    """`pg_dump` without Postgres: record argv + the env it was handed, and
    write `ARCHIVE` to the file it was told to produce."""
    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        seen["env"] = kw.get("env")
        with open(argv[argv.index("-f") + 1], "wb") as fh:
            fh.write(ARCHIVE)
        return _Proc()

    monkeypatch.setattr(snapshot_mod.subprocess, "run", fake_run)
    return seen


# --- the happy path -----------------------------------------------------------


def test_signed_request_against_a_live_primed_stream_returns_the_archive(
    app_and_sessions, fake_pg_dump
):
    app, sessions = app_and_sessions
    _prime(sessions, secret="s3cret-stream-key")

    resp = _signed(app, _body(), "s3cret-stream-key")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/octet-stream")
    assert resp.content == ARCHIVE
    argv = fake_pg_dump["argv"]
    assert argv[:4] == ["pg_dump", "-Fc", "--no-owner", "--no-privileges"]
    assert argv[4] == "-f"
    # The last arg is the DB URL, read off the session's own engine bind — no
    # extra mount-site argument, and no remote host anywhere in it.
    assert argv[-1].startswith("sqlite:")


def test_the_dump_targets_the_services_own_bound_database(app_and_sessions, monkeypatch):
    """The route reads its DB URL from the session's engine bind, so governance,
    memory and pm pick the route up with NO change at their mount sites — and a
    password in that URL rides PGPASSWORD, never argv (`ps` is world-readable)."""
    app, sessions = app_and_sessions
    _prime(sessions, secret="k")
    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        seen["env"] = kw.get("env")
        with open(argv[argv.index("-f") + 1], "wb") as fh:
            fh.write(ARCHIVE)
        return _Proc()

    monkeypatch.setattr(snapshot_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        snapshot_mod,
        "database_url",
        lambda session: make_url("postgresql+psycopg://sean:sekret@127.0.0.1:5432/snowline_governance"),
    )

    assert _signed(app, _body(), "k").status_code == 200
    assert seen["argv"][-1] == "postgresql://sean@127.0.0.1:5432/snowline_governance"
    assert "sekret" not in " ".join(seen["argv"])
    assert seen["env"]["PGPASSWORD"] == "sekret"


# --- refusals: all byte-identical 404s ---------------------------------------


def test_every_refusal_is_the_same_404_a_missing_route_gives(
    app_and_sessions, fake_pg_dump
):
    """Non-enumeration: unsigned, mis-signed, wrong-peer, wrong-epoch, retired,
    and no-such-stream must be INDISTINGUISHABLE from a route that isn't there."""
    app, sessions = app_and_sessions
    _prime(sessions, secret="s3cret-stream-key")
    baseline = _request(app, "POST", "/replication-admin/no-such-route", content=b"{}")
    assert baseline.status_code == 404

    body = _body()
    refusals = [
        # no signature header at all
        _request(app, "POST", SNAPSHOT_PATH, content=body),
        # a signature under the wrong secret
        _signed(app, body, "not-the-stream-secret"),
        # correctly signed, but naming a stream that does not exist
        _signed(app, _body(source_id="primary.memory"), "s3cret-stream-key"),
        # correctly signed, but a DIFFERENT peer than the subscription targets
        _signed(app, _body(peer_source_id="someone-else.governance"), "s3cret-stream-key"),
        # correctly signed, but a retired epoch
        _signed(app, _body(epoch="epoch-old"), "s3cret-stream-key"),
        # a body that isn't even JSON
        _request(app, "POST", SNAPSHOT_PATH, content=b"not json"),
    ]
    for resp in refusals:
        assert resp.status_code == baseline.status_code
        assert resp.content == baseline.content
        assert resp.headers["content-type"] == baseline.headers["content-type"]
    assert "argv" not in fake_pg_dump  # pg_dump never ran for any of them


def test_a_retired_subscriptions_secret_no_longer_opens_the_snapshot(
    app_and_sessions, fake_pg_dump
):
    """The `--reseed` guard: a fresh-epoch re-seed retires the old stream before
    priming the new one, so only the NEW epoch's secret works. `active` is what
    makes that true."""
    app, sessions = app_and_sessions
    _prime(sessions, secret="old-secret", epoch="epoch-old", active=False)
    assert _signed(app, _body(epoch="epoch-old"), "old-secret").status_code == 404

    _prime(sessions, secret="new-secret", epoch="epoch-new")
    assert _signed(app, _body(epoch="epoch-new"), "new-secret").status_code == 200


def test_the_trust_gate_still_runs_first(app_and_sessions, fake_pg_dump):
    """`_require_trusted` is unchanged and applies BEFORE the stream check — an
    untrusted peer gets the ordinary 403, not the non-enumeration 404, because it
    never reached the authorization at all (§5.1)."""
    app, sessions = app_and_sessions
    _prime(sessions, secret="s3cret-stream-key")
    resp = _signed(app, _body(), "s3cret-stream-key", peer=HOTEL_LAN_PEER)
    assert resp.status_code == 403
    assert "argv" not in fake_pg_dump


def test_the_stream_secret_is_never_echoed(app_and_sessions, fake_pg_dump):
    app, sessions = app_and_sessions
    _prime(sessions, secret="s3cret-stream-key")
    for resp in (
        _signed(app, _body(), "s3cret-stream-key"),
        _signed(app, _body(), "wrong"),
    ):
        assert b"s3cret-stream-key" not in resp.content


def test_a_failed_pg_dump_is_a_500_not_a_truncated_archive(app_and_sessions, monkeypatch):
    """A partial archive restored over the spoke's store is §7's silent
    data-loss mode — any non-zero pg_dump exit must fail the request."""
    app, sessions = app_and_sessions
    _prime(sessions, secret="k")

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "pg_dump: error: connection to server failed"

    monkeypatch.setattr(snapshot_mod.subprocess, "run", lambda argv, **kw: _Proc())
    resp = _signed(app, _body(), "k")
    assert resp.status_code == 500
    assert "pg_dump" in resp.json()["detail"]


# --- the shared URL/password helper ------------------------------------------


def test_libpq_url_normalization():
    assert snapshot_mod.libpq_url("postgresql+psycopg:///db") == "postgresql:///db"
    assert snapshot_mod.libpq_url("postgresql:///db") == "postgresql:///db"


def test_libpq_url_and_env_lifts_password_off_argv():
    """The hygiene BOTH sides share (the service's pg_dump and the seed's
    pg_restore): passwords ride PGPASSWORD, never argv (visible in `ps`)."""
    url, env = snapshot_mod.libpq_url_and_env(
        "postgresql+psycopg://user:sekret@host:5432/db"
    )
    assert env == {"PGPASSWORD": "sekret"}
    assert "sekret" not in url
    assert url == "postgresql://user@host:5432/db"
    # No password (socket/peer auth) → empty overlay, url unchanged.
    assert snapshot_mod.libpq_url_and_env("postgresql:///db") == ("postgresql:///db", {})


def test_libpq_url_and_env_accepts_a_sqlalchemy_url():
    """The service side hands it an engine `URL` object, not a string — and a
    percent-escaped password comes back DECODED, as libpq wants."""
    url, env = snapshot_mod.libpq_url_and_env(
        make_url("postgresql+psycopg://user:se%2Fkret@host:5432/db")
    )
    assert url == "postgresql://user@host:5432/db"
    assert env == {"PGPASSWORD": "se/kret"}
    assert snapshot_mod.libpq_url_and_env(make_url("postgresql:///db")) == (
        "postgresql:///db",
        {},
    )
