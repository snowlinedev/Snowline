"""Platform boot observability (issue #39): the registry is in-memory, so a
restart boots it empty and every mounted surface serves zero tools until the
plugins' registration heartbeats re-upsert them. That window must be LOUD — a
crash-restart under launchd otherwise looks healthy while the gateway is hollow.
"""

from __future__ import annotations

import logging

from starlette.testclient import TestClient

from snowline_platform.app import create_app
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry
from snowline_platform.trust import Principal, TrustResolver


class _AlwaysTrust:
    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _app(registry: PluginRegistry | None = None):
    return create_app(
        resolver=TrustResolver([_AlwaysTrust()]),
        registry=registry,
        migrate_on_startup=False,
    )


def test_boot_with_empty_registry_warns_loudly(caplog):
    with caplog.at_level(logging.WARNING, logger="snowline_platform.app"):
        with TestClient(_app()):  # entering the client runs the lifespan
            pass
    assert any(
        "ZERO plugins registered" in r.message for r in caplog.records
    ), caplog.text


def test_boot_with_registered_plugin_does_not_warn(caplog):
    reg = PluginRegistry()
    reg.upsert(
        PluginManifest(
            name="governance",
            base_url="http://127.0.0.1:8801",
            surfaces={"/mcp": "main"},
        )
    )
    with caplog.at_level(logging.WARNING, logger="snowline_platform.app"):
        with TestClient(_app(registry=reg)):
            pass
    assert not any("ZERO plugins registered" in r.message for r in caplog.records)
