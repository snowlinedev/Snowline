"""In-memory registry of the plugins the platform knows about.

Registration is how a plugin joins the platform WITHOUT a restart — the gateway
and the health checker read this registry to compose and monitor plugins. Each
entry pairs a plugin's manifest with its runtime status (set by the health
checker). In-memory is fine for a single platform process; persistence can come
later if the platform itself needs to survive restarts with its plugin set.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum

from snowline_platform.manifest import PluginManifest


class PluginStatus(str, Enum):
    UNKNOWN = "unknown"  # registered, not yet health-checked
    UP = "up"
    DOWN = "down"  # crashed, unhealthy, or unreachable — gateway routes around


@dataclass
class RegisteredPlugin:
    manifest: PluginManifest
    status: PluginStatus = PluginStatus.UNKNOWN


class PluginAlreadyRegistered(Exception):
    """A plugin with this name is already registered (and replace was not set)."""


class PluginNotFound(Exception):
    """No plugin is registered under this name."""


class PluginRegistry:
    """Thread-safe in-memory map of plugin name -> RegisteredPlugin."""

    def __init__(self) -> None:
        self._plugins: dict[str, RegisteredPlugin] = {}
        self._lock = threading.Lock()

    def register(
        self, manifest: PluginManifest, *, replace: bool = False
    ) -> RegisteredPlugin:
        with self._lock:
            if manifest.name in self._plugins and not replace:
                raise PluginAlreadyRegistered(manifest.name)
            entry = RegisteredPlugin(manifest=manifest)
            self._plugins[manifest.name] = entry
            return entry

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

    def set_status(self, name: str, status: PluginStatus) -> None:
        """Update a plugin's runtime status (used by the health checker).

        A no-op if the plugin was unregistered in the meantime — the health
        checker shouldn't resurrect a removed entry."""
        with self._lock:
            entry = self._plugins.get(name)
            if entry is not None:
                entry.status = status
