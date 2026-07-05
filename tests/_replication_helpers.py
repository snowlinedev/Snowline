"""Shared harness for the replication pairing + seed tests (issue #82).

Stands up plugin-shaped and platform-shaped ASGI apps over the SDK's
`build_replication_router` (the real admin surface the CLI drives), each on its
own store, and a synchronous `RoutedClient` that dispatches by URL host to the
right app — so the pairing/seed libraries run UNCHANGED against in-process
instances, exactly as they would against two real hosts over the tailnet. The
per-request event loop + loopback peer IP mirror `sdk/tests/test_replication_admin.py`.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlparse

import anyio
import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from snowline_plugin_sdk.contract import CONTRACT_VERSION
from snowline_plugin_sdk.replication.admin import build_replication_router
from snowline_plugin_sdk.replication.models import ReplicationBase
from snowline_platform.replication import SCOPE_EVENTS

# A loopback peer so the SDK admin surface's tailnet gate admits every routed
# request (§5.1: behind the serve→loopback front, cross-instance traffic arrives
# on loopback).
_PEER = ("127.0.0.1", 4242)


def make_store(db_url: str | None = None):
    """An (engine, sessionmaker, session_scope) triple over a fresh replication
    store. `db_url=None` is in-memory SQLite pinned by StaticPool; a `sqlite:///`
    file URL lets a separate engine (the seed's scrub/inject) reach the same DB."""
    if db_url:
        engine = create_engine(db_url, future=True)
    else:
        engine = create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            future=True,
        )
    ReplicationBase.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def scope():
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return engine, maker, scope


def make_participant(*, ingest_path: str = "/events/ingest", db_url: str | None = None):
    """A plugin-shaped app: the SDK replication router over a fresh store with a
    recording apply seam. Returns a namespace (app, scope, sessionmaker, engine,
    applied)."""
    engine, maker, scope = make_store(db_url)
    applied: list[dict] = []

    def apply(session, envelope):
        applied.append(envelope)

    app = FastAPI()
    app.include_router(build_replication_router(scope, apply, ingest_path=ingest_path))
    return SimpleNamespace(
        app=app, scope=scope, sessionmaker=maker, engine=engine, applied=applied
    )


def make_platform(*, plugins: list[dict], db_url: str | None = None,
                  ingest_path: str = "/replication/events/ingest",
                  platform_contract_version: int = CONTRACT_VERSION,
                  platform_events: tuple[str, ...] = SCOPE_EVENTS,
                  platform_advertised_base_url: str | None = None):
    """A platform-shaped app: a participant app (the platform's own scope stream)
    PLUS a `GET /plugins` registry returning `plugins` (each a
    `{name,status,manifest}` entry the way `plugins_routes` does) PLUS the §8
    replication self-manifest at `GET /replication/manifest` (issue #95) the
    pairing CLI reads to version-check the platform stream. The
    `platform_contract_version`/`platform_events` knobs let a test skew the
    platform's declared contract across the two sides, exactly as `plugin_entry`
    does for a plugin."""
    inst = make_participant(ingest_path=ingest_path, db_url=db_url)

    @inst.app.get("/plugins")
    async def list_plugins() -> dict:  # noqa: D401 - test fixture route
        return {"plugins": plugins}

    @inst.app.get("/replication/manifest")
    async def replication_manifest() -> dict:  # noqa: D401 - test fixture route
        return {
            "contract_version": platform_contract_version,
            "ingest_path": ingest_path,
            "events": list(platform_events),
            "advertised_base_url": platform_advertised_base_url,
        }

    return inst


def plugin_entry(name: str, base_url: str, *, ingest_path: str = "/events/ingest",
                 contract_version: int = 2, events: list[str] | None = None,
                 advertised_base_url: str | None = None) -> dict:
    """A `/plugins` registry entry whose manifest carries a `replication` block —
    what the pairing CLI reads to discover an opted-in plugin. Pass
    `advertised_base_url` to exercise the §4.1 advertised-address preference."""
    block: dict = {
        "contract_version": contract_version,
        "ingest_path": ingest_path,
        "events": events if events is not None else [f"{name}.recorded"],
    }
    if advertised_base_url is not None:
        block["advertised_base_url"] = advertised_base_url
    return {
        "name": name,
        "status": "up",
        "manifest": {
            "name": name,
            "base_url": base_url,
            "replication": block,
        },
    }


class RoutedClient:
    """A synchronous httpx-shaped client that dispatches by URL host to an ASGI
    app. `.get`/`.post` take absolute URLs (as the pairing/seed libraries emit);
    each call runs the target app over `httpx.ASGITransport` under a private
    event loop with a loopback peer IP. Supports the same kwargs the libraries
    and the SDK delivery loop use (`json=`, `content=`, `headers=`)."""

    def __init__(self, mounts: dict[str, FastAPI]):
        self._mounts = mounts

    def __enter__(self) -> "RoutedClient":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self._request("POST", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        host = urlparse(url).netloc
        app = self._mounts.get(host)
        if app is None:
            raise AssertionError(f"no mounted app for host {host!r} (url {url})")

        async def main():
            transport = httpx.ASGITransport(app=app, client=_PEER)
            async with httpx.AsyncClient(
                transport=transport, base_url=f"http://{host}"
            ) as client:
                return await client.request(method, url, **kwargs)

        return anyio.run(main)
