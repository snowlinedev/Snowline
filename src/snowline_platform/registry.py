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

from snowline_platform.manifest import (
    HttpSurface,
    PluginManifest,
    http_prefix_matches,
    http_prefixes_collide,
)

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


class HttpPrefixConflict(Exception):
    """A manifest declared an `http` prefix another plugin already holds
    (gateway.md §3a).

    Root-level http prefixes are a SHARED namespace across plugins, so the
    registry owns the invariant: two plugins can never hold colliding
    prefixes. Carries the offending `prefix` and the `holder` (and the
    conflicting prefix the holder declared, when it is a containment rather
    than an exact match) so the route can name both in its 409 and its
    WARNING."""

    def __init__(self, prefix: str, holder: str, holder_prefix: str) -> None:
        self.prefix = prefix
        self.holder = holder
        self.holder_prefix = holder_prefix
        super().__init__(
            f"http prefix {prefix!r} collides with {holder_prefix!r}, already "
            f"held by plugin {holder!r}"
        )


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
            # The registry owns the http-prefix invariant (gateway.md §3a):
            # a prefix is a ROOT-LEVEL, cross-plugin-shared namespace, so it
            # is checked HERE — where the whole plugin set is visible and the
            # write is serialized under the lock — not in the route. The
            # plugin's OWN prefixes are excluded from the comparison, so a
            # heartbeat re-POST and a redeploy that re-shapes its own
            # prefixes both pass; only another plugin's claim refuses.
            self._assert_http_prefixes_free(manifest)
            entry = RegisteredPlugin(manifest=manifest)
            self._plugins[manifest.name] = entry
            return entry, ("updated" if existing is not None else "created")

    def _assert_http_prefixes_free(self, manifest: PluginManifest) -> None:
        """Raise `HttpPrefixConflict` if any of `manifest`'s http prefixes
        collides with one held by a DIFFERENT registered plugin. Callers hold
        `self._lock`."""
        for surface in manifest.http:
            for name, entry in self._plugins.items():
                if name == manifest.name:
                    continue
                for held in entry.manifest.http:
                    if http_prefixes_collide(surface.prefix, held.prefix):
                        raise HttpPrefixConflict(
                            surface.prefix, name, held.prefix
                        )

    def http_route(self, path: str) -> tuple[RegisteredPlugin, HttpSurface] | None:
        """Resolve a request path to the plugin + surface that serves it, by
        LONGEST declared prefix.

        Collisions are refused at registration, so in practice at most one
        prefix ever matches a given path — longest-match is nonetheless the
        stated rule, so resolution stays defined (and testable) rather than
        dependent on registry iteration order. Matching is segment-aligned
        (`http_prefix_matches`): `/providerx` does not match `/provider`.
        `None` when no plugin declared a prefix covering `path`."""
        best: tuple[RegisteredPlugin, HttpSurface] | None = None
        best_len = -1
        with self._lock:
            for entry in self._plugins.values():
                for surface in entry.manifest.http:
                    if (
                        http_prefix_matches(surface.prefix, path)
                        and len(surface.prefix) > best_len
                    ):
                        best = (entry, surface)
                        best_len = len(surface.prefix)
        return best

    def http_routes(self) -> list[tuple[str, HttpSurface]]:
        """Every declared http prefix as `(plugin name, surface)`, sorted by
        prefix — the operator's view of the proxied surface map
        (`GET /plugins/http-routes`)."""
        with self._lock:
            pairs = [
                (entry.manifest.name, surface)
                for entry in self._plugins.values()
                for surface in entry.manifest.http
            ]
        return sorted(pairs, key=lambda pair: pair[1].prefix)

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
