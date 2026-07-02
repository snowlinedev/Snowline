"""In-memory registry of the plugins the platform knows about.

Registration is how a plugin joins the platform WITHOUT a restart — the gateway
and the health checker read this registry to compose and monitor plugins. Each
entry pairs a plugin's manifest with its runtime status (set by the health
checker). In-memory is fine for a single platform process BECAUSE plugins
re-assert membership on a registration heartbeat (issue #39): a platform restart
empties the registry, and every plugin re-upserts itself within one beat.
Persistence can still come later if that window ever matters.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from snowline_platform.manifest import PluginManifest

UpsertOutcome = Literal["created", "unchanged", "updated"]


class PluginStatus(str, Enum):
    UNKNOWN = "unknown"  # registered, not yet health-checked
    UP = "up"
    DOWN = "down"  # crashed, unhealthy, or unreachable — gateway routes around


@dataclass
class RegisteredPlugin:
    manifest: PluginManifest
    status: PluginStatus = PluginStatus.UNKNOWN


class PluginNotFound(Exception):
    """No plugin is registered under this name."""


class PluginRegistry:
    """Thread-safe in-memory map of plugin name -> RegisteredPlugin."""

    def __init__(self) -> None:
        self._plugins: dict[str, RegisteredPlugin] = {}
        self._lock = threading.Lock()

    def upsert(self, manifest: PluginManifest) -> tuple[RegisteredPlugin, UpsertOutcome]:
        """Idempotent register — the ONE write verb, and the heartbeat's verb
        (issue #39).

        Returns ``(entry, outcome)`` where outcome is:
          * ``"created"``   — first registration; fresh entry, status UNKNOWN.
          * ``"unchanged"`` — an identical manifest is already registered; the
            existing entry is KEPT, so the health poller's status survives the
            beat (a heartbeat must not flap UP back to UNKNOWN every interval).
          * ``"updated"``   — the name is registered with a DIFFERENT manifest
            (a redeploy changed base_url/surfaces/...); the entry is replaced
            and status resets to UNKNOWN — the old status described a plugin
            that may now live elsewhere.
        """
        with self._lock:
            existing = self._plugins.get(manifest.name)
            if existing is not None and existing.manifest == manifest:
                return existing, "unchanged"
            entry = RegisteredPlugin(manifest=manifest)
            self._plugins[manifest.name] = entry
            return entry, ("updated" if existing is not None else "created")

    def unregister(self, name: str) -> None:
        with self._lock:
            if name not in self._plugins:
                raise PluginNotFound(name)
            del self._plugins[name]

    def get(self, name: str) -> RegisteredPlugin:
        with self._lock:
            try:
                return self._plugins[name]
            except KeyError:
                raise PluginNotFound(name) from None

    def list(self) -> list[RegisteredPlugin]:
        with self._lock:
            return list(self._plugins.values())

    def set_status(
        self,
        name: str,
        status: PluginStatus,
        *,
        expected_entry: RegisteredPlugin | None = None,
    ) -> None:
        """Update a plugin's runtime status (used by the health checker).

        A no-op if the plugin was unregistered in the meantime — the health
        checker shouldn't resurrect a removed entry. When `expected_entry` is
        given, also a no-op if the registered entry is a DIFFERENT object: a
        health result probed against an old manifest must not be stamped onto
        an entry that an `updated` upsert replaced mid-round (the new address
        was never checked; the poller re-verifies it next round)."""
        with self._lock:
            entry = self._plugins.get(name)
            if entry is None:
                return
            if expected_entry is not None and entry is not expected_entry:
                return
            entry.status = status
