"""Remember which plugins were registered, across platform restarts, and hold
`tools/list` briefly after a restart until they are back (issue #240, part B).

The `PluginRegistry` is in-memory: a restart empties it and plugins re-register
on their heartbeat (issue #39, default every 15s). A client that reconnects and
lists in that window gets a partial tool list. Clients that honour
`tools/list_changed` recover (`gateway_notify`); this is the safety net for
those that don't.

* `RegistryStateFile` — a registry observer that writes the set of registered
  plugin names to a small JSON file on every membership change, atomically
  (temp file + rename). Write failures are logged, never raised.
* `StartupGrace` — read at boot from that file. For `window` seconds after the
  platform starts serving, `wait()` (awaited at the top of every surface
  `tools/list`) polls until every previously-registered plugin is back or the
  window ends. No file (first boot, or the feature unconfigured) → no wait.
  Outside the window → no wait, ever.

File format::

    {"version": 1, "plugins": ["governance", "memory", "platform", "pm"]}
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

from snowline_platform.registry import PluginRegistry, RegistryChange

log = logging.getLogger("snowline_platform.registry_state")

STATE_VERSION = 1

# How often a waiting `tools/list` re-checks the registry during the grace.
# Module-level so tests can shrink it.
POLL_SECONDS: float = 0.1


def read_registered_names(path: Path) -> frozenset[str]:
    """The plugin names recorded in the state file at `path`; empty when the
    file is missing or unreadable (a corrupt file must never block boot — it
    only costs the grace)."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        log.warning("registry state: cannot read %s: %s", path, exc)
        return frozenset()
    try:
        data = json.loads(raw)
        names = data["plugins"]
        if not isinstance(names, list) or not all(
            isinstance(n, str) for n in names
        ):
            raise ValueError("'plugins' is not a list of strings")
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("registry state: ignoring malformed %s: %s", path, exc)
        return frozenset()
    return frozenset(names)


def write_registered_names(path: Path, names: frozenset[str] | set[str]) -> None:
    """Atomically write `names` to `path` (creating the parent directory).
    Raises on failure; `RegistryStateFile` is the never-raises wrapper."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": STATE_VERSION, "plugins": sorted(names)}, indent=2
    )
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class StartupGrace:
    """Bounded post-restart wait for previously-registered plugins.

    `expected` is the name set read from the state file at boot. The window
    opens at construction and is re-opened by `start()` (called when the
    lifespan begins serving, so a slow boot-migrate doesn't eat it)."""

    def __init__(
        self,
        registry: PluginRegistry,
        expected: frozenset[str],
        window: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self.expected = expected
        self.window = window
        self._clock = clock
        self._deadline = clock() + window
        self._logged = False

    def start(self) -> None:
        self._deadline = self._clock() + self.window

    def active(self) -> bool:
        return bool(self.expected) and self._clock() < self._deadline

    def pending(self) -> frozenset[str]:
        """Expected plugins not yet re-registered — empty once the window has
        closed, so the grace can never outlive it."""
        if not self.active():
            return frozenset()
        present = {e.manifest.name for e in self._registry.list()}
        return self.expected - present

    async def wait(self) -> None:
        """Return once every expected plugin is registered or the window ends.
        Never waits outside the window; never raises."""
        missing = self.pending()
        if not missing:
            return
        if not self._logged:
            self._logged = True
            log.info(
                "startup grace: holding tools/list up to %.1fs for "
                "previously-registered plugin(s) %s",
                max(0.0, self._deadline - self._clock()),
                sorted(missing),
            )
        while missing:
            remaining = self._deadline - self._clock()
            if remaining <= 0:
                break
            await anyio.sleep(min(POLL_SECONDS, remaining))
            missing = self.pending()
        if missing:
            log.warning(
                "startup grace: window ended with plugin(s) %s still not "
                "re-registered; answering tools/list without them",
                sorted(missing),
            )


class RegistryStateFile:
    """Registry observer persisting the registered-plugin name set to `path`.

    Writes on membership changes only (create/update/remove — a status flip
    doesn't change membership). While a `StartupGrace` is still pending, the
    still-missing expected names are kept in the file too, so a second restart
    inside the window doesn't forget plugins that hadn't made it back yet."""

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
        if change.kind == "status":
            return
        self.write()

    def write(self) -> None:
        names = {e.manifest.name for e in self._registry.list()}
        if self._grace is not None:
            names |= self._grace.pending()
        try:
            write_registered_names(self.path, frozenset(names))
        except OSError as exc:
            log.warning(
                "registry state: failed to write %s: %s", self.path, exc
            )
