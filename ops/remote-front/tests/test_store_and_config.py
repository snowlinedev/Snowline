"""Config parsing (fail-loud on the load-bearing values) and the persistent
store's restart survival (a redeploy must not force re-adding the connector), plus
a light import-purity guard (the app never imports the platform, fly, or
tailscale)."""

from __future__ import annotations

import sys

import anyio
import pytest

from snowline_remote_front.config import Config, ConfigError
from snowline_remote_front.store import SqliteStore

from ._helpers import (
    REDIRECT_URI,
    build_config,
    build_front,
    front_client,
    full_grant,
)


def test_config_from_env_requires_load_bearing_values():
    with pytest.raises(ConfigError):
        Config.from_env({})  # nothing set
    with pytest.raises(ConfigError):
        # upstream + owner password still missing
        Config.from_env({"REMOTE_FRONT_ISSUER_URL": "https://x.fly.dev"})


def test_config_from_env_defaults_resource_and_generates_signing_key():
    cfg = Config.from_env(
        {
            "REMOTE_FRONT_ISSUER_URL": "https://x.fly.dev/",
            "REMOTE_FRONT_UPSTREAM": "http://mini.ts.net:8850/remote/mcp",
            "REMOTE_FRONT_OWNER_PASSWORD": "hunter2",
        }
    )
    assert cfg.issuer_url == "https://x.fly.dev"
    assert cfg.resource_url == "https://x.fly.dev/mcp"
    assert cfg.resource_path == "/mcp"
    # No signing key supplied → an ephemeral one is generated (never empty).
    assert cfg.signing_key


def test_sqlite_store_survives_restart(tmp_path):
    """A client registration + refresh token issued against a SqliteStore are
    still there after the app is torn down and rebuilt on the SAME file — the
    connector keeps working across a redeploy without re-auth."""
    db = str(tmp_path / "remote-front.db")
    config = build_config()

    async def _grant(app):
        async with front_client(app) as client:
            return await full_grant(client)

    app1 = build_front(config=config, store=SqliteStore(db))
    grant = anyio.run(_grant, app1)
    client_id = grant["registration"]["client_id"]
    refresh = grant["tokens"]["refresh_token"]

    # "Restart": a fresh app + fresh store object on the same DB file.
    store2 = SqliteStore(db)
    assert store2.get_client(client_id) is not None
    assert store2.get_refresh_token(refresh) is not None

    # And the reopened store can service a refresh (connector recovers post-restart).
    app2 = build_front(config=config, store=store2)

    async def _refresh(app):
        async with front_client(app) as client:
            return await client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": client_id,
                    "client_secret": grant["registration"]["client_secret"],
                },
            )

    resp = anyio.run(_refresh, app2)
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]


def test_import_purity_no_platform_fly_or_tailscale():
    # Importing the app must not drag in the Snowline platform, fly, or tailscale
    # (issue #120: zero platform code changes; fly/tailscale are DEPLOY-only).
    # Run in a FRESH interpreter — in the combined workspace test run the
    # platform's own tests have already imported `snowline_platform` into this
    # process's sys.modules, so a global check here would be polluted. A
    # subprocess isolates "what does importing THIS package pull in".
    import subprocess

    code = (
        "import sys; import snowline_remote_front; from snowline_remote_front import app;"
        "bad=[m for m in sys.modules if m.split('.')[0] in {'snowline_platform','fly','tailscale'}];"
        "assert not bad, bad; print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
