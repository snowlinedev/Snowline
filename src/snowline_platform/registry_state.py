"""Remember which plugins were registered, across platform restarts, and hold
a surface's `tools/list` briefly after a restart until they are back (issue
#240).

The `PluginRegistry` is in-memory: a restart empties it and plugins re-register
on their heartbeat (issue #39, default every 15s). A client that reconnects and
lists tools in that window gets a partial list, and the stateless gateway
surfaces cannot tell it later that the list grew. The grace closes that window
for the common case: the client's first list after a restart waits for the
plugins it is going to need.

* `RegistryStateFile` — a registry observer that writes the registered
  plugins' MANIFESTS to a small JSON file on every membership change,
  atomically (temp file + fsync + rename). Write failures are logged, never
  raised.
* `StartupGrace` — built at boot from that file. For `window` seconds after the
  platform starts serving, `wait(surface, allowlist)` (awaited at the top of
  every surface `tools/list`) polls until every previously-registered plugin
  that would serve THAT surface is back, or the window ends. Which plugins serve
  a surface is decided by the gateway's own `discover_upstreams` (allowlist +
  projection included) run over the persisted manifests — which is why
  manifests, not just names, are persisted. No file (first boot, or the feature
  unconfigured) → no wait. Outside the window → no wait, ever.
* When the window closes, `expire` rewrites the file from the CURRENT registry,
  so a plugin that never came back ("ghost") is forgotten instead of costing
  every later restart a full window.

File format::

    {"version": 1, "plugins": {"governance": {<PluginManifest JSON>}, ...}}
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import anyio
from pydantic import ValidationError

from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry, RegistryChange

log = logging.getLogger("snowline_platform.registry_state")

STATE_VERSION = 1

# How often a waiting `tools/list` re-checks the registry during the grace.
# Module-level so tests can shrink it.
POLL_SECONDS: float = 0.1


def read_registered(path: Path) -> dict[str, PluginManifest]:
    """The plugin manifests recorded in the state file at `path`, by name;
    empty when the file is missing or unreadable (a corrupt file must never
    block boot — it only costs the grace). A single malformed entry is skipped,
    not fatal."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("registry state: cannot read %s: %s", path, exc)
        return {}
    try:
        plugins = json.loads(raw)["plugins"]
        if not isinstance(plugins, dict):
            raise TypeError("'plugins' is not an object")
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("registry state: ignoring malformed %s: %s", path, exc)
        return {}
    out: dict[str, PluginManifest] = {}
    for name, data in plugins.items():
        try:
            manifest = PluginManifest.model_validate(data)
        except ValidationError as exc:
            log.warning(
                "registry state: skipping malformed entry %r in %s: %s",
                name,
                path,
                exc,
            )
            continue
        out[manifest.name] = manifest
    return out


def write_registered(path: Path, manifests: list[PluginManifest]) -> None:
    """Atomically write `manifests` to `path` (creating the parent directory).
    Raises on failure; `RegistryStateFile` is the never-raises wrapper."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "version": STATE_VERSION,
            "plugins": {
                m.name: m.model_dump(mode="json")
                for m in sorted(manifests, key=lambda m: m.name)
            },
        },
        indent=2,
    )
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class StartupGrace:
    """Bounded post-restart wait for previously-registered plugins.

    `expected` is the manifest set read from the state file at boot. The window
    opens at construction and is re-opened by `start()` (called when the
    lifespan begins serving, so a slow boot-migrate doesn't eat it)."""

    def __init__(
        self,
        registry: PluginRegistry,
        expected: dict[str, PluginManifest],
        window: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self.expected = dict(expected)
        self.window = window
        self._clock = clock
        self._deadline = clock() + window
        self._by_surface: dict[tuple[str, frozenset[str] | None], frozenset[str]] = {}
        self._logged: set[str] = set()

    def start(self) -> None:
        self._deadline = self._clock() + self.window

    def active(self) -> bool:
        return bool(self.expected) and self._clock() < self._deadline

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def _present(self) -> set[str]:
        return {e.manifest.name for e in self._registry.list()}

    def pending(self) -> frozenset[str]:
        """Expected plugins (any surface) not yet re-registered — empty once
        the window has closed, so the grace can never outlive it."""
        if not self.active():
            return frozenset()
        return frozenset(self.expected) - self._present()

    def _serving(
        self, surface: str, allowlist: frozenset[str] | None
    ) -> frozenset[str]:
        """Which expected plugins would serve `surface` — the gateway's own
        discovery over the persisted manifests, so allowlist + projection match
        exactly what the live surface will compose."""
        key = (surface, allowlist)
        names = self._by_surface.get(key)
        if names is None:
            # Local import: `gateway` imports `registry`, keep this module light.
            from snowline_platform.gateway import discover_upstreams

            shadow_registry = PluginRegistry()
            for manifest in self.expected.values():
                try:
                    shadow_registry.upsert(manifest)
                except Exception as exc:  # noqa: BLE001 — stale state must not break a list
                    log.warning(
                        "startup grace: cannot model persisted plugin %r: %s",
                        manifest.name,
                        exc,
                    )
            names = frozenset(
                u.plugin_name
                for u in discover_upstreams(shadow_registry, surface, allowlist)
            )
            self._by_surface[key] = names
        return names

    def pending_for(
        self, surface: str, allowlist: frozenset[str] | None = None
    ) -> frozenset[str]:
        """Expected plugins that would serve `surface` and are not back yet;
        empty outside the window."""
        if not self.active():
            return frozenset()
        return self._serving(surface, allowlist) - self._present()

    async def wait(
        self, surface: str, allowlist: frozenset[str] | None = None
    ) -> None:
        """Return once every expected plugin serving `surface` is registered or
        the window ends. Never waits outside the window; never raises."""
        missing = self.pending_for(surface, allowlist)
        if not missing:
            return
        if surface not in self._logged:
            self._logged.add(surface)
            log.info(
                "startup grace: holding %r tools/list up to %.1fs for "
                "previously-registered plugin(s) %s",
                surface,
                self.remaining(),
                sorted(missing),
            )
        while missing:
            remaining = self.remaining()
            if remaining <= 0:
                break
            await anyio.sleep(min(POLL_SECONDS, remaining))
            missing = self.pending_for(surface, allowlist)
        if missing:
            log.warning(
                "startup grace: window ended with plugin(s) %s still not "
                "re-registered; answering %r tools/list without them",
                sorted(missing),
                surface,
            )

    async def expire(self, on_expire: Callable[[], None]) -> None:
        """Sleep until the window closes, then call `on_expire` (the state
        file's rewrite — by then `pending()` is empty, so plugins that never
        came back are dropped from the file). Run for the app lifespan."""
        if not self.expected:
            return
        while (remaining := self.remaining()) > 0:
            await anyio.sleep(remaining)
        missing = frozenset(self.expected) - self._present()
        if missing:
            log.warning(
                "startup grace: forgetting plugin(s) %s that did not "
                "re-register within %.0fs of boot",
                sorted(missing),
                self.window,
            )
        on_expire()


class RegistryStateFile:
    """Registry observer persisting the registered plugins' manifests to `path`.

    While a `StartupGrace` is still pending, the still-missing expected plugins
    are kept in the file too, so a second restart inside the window doesn't
    forget plugins that hadn't made it back yet. Once the window closes,
    `write()` records exactly the live registry."""

    def __init__(
        self,
        registry: PluginRegistry,
        path: Path,
        grace: StartupGrace | None = None,
    ) -> None:
        self._registry = registry
        self.path = path
        self._grace = grace

    def registry_changed(self, change: RegistryChange) -> None:
        self.write()

    def write(self) -> None:
        manifests = {e.manifest.name: e.manifest for e in self._registry.list()}
        if self._grace is not None:
            for name in self._grace.pending():
                manifests.setdefault(name, self._grace.expected[name])
        try:
            write_registered(self.path, list(manifests.values()))
        except OSError as exc:
            log.warning(
                "registry state: failed to write %s: %s", self.path, exc
            )
