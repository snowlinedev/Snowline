"""Push `notifications/tools/list_changed` to clients of a composed surface
(issue #240).

A gateway surface's tool list is whatever its live upstreams expose, so it
changes whenever the registry does: a plugin registers, re-registers with a
changed manifest, is removed, or flips across the DOWN boundary. Before #240 the
surfaces ran a STATELESS streamable-HTTP transport — no client session, no
standalone GET stream — so the server could not tell a connected client its
list had grown. A client that (re)connected and listed during the post-restart
re-registration window kept the partial list for the rest of its session.

Two pieces fix that, both MCP-side so `registry` stays free of MCP imports:

* `ListChangedServer` — the low-level `Server` for a surface. It advertises
  ``tools.listChanged`` and tracks the live `ServerSession`s that have listed
  tools (the only sessions that hold a list that can go stale).
* `SurfaceChangeNotifier` — a registry observer per surface. On each registry
  change it re-discovers the surface's upstream set and, if it differs from the
  last one seen, marks the surface dirty; a background task coalesces dirty
  marks into at most one broadcast per `DEBOUNCE_SECONDS` and sends
  `tools/list_changed` to every tracked session, concurrently, each bounded and
  isolated.

Clients that ignore `list_changed` are unaffected: the notification is a plain
server→client JSON-RPC notification they may drop.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from dataclasses import dataclass, field

import anyio
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession

from snowline_platform.gateway import Upstream, discover_upstreams
from snowline_platform.registry import PluginRegistry, RegistryChange

log = logging.getLogger("snowline_platform.gateway_notify")

# Coalescing window: a burst of registry changes (every plugin re-registering
# seconds after a platform restart) becomes ONE notification per surface per
# window. Module-level so tests can shrink it.
DEBOUNCE_SECONDS: float = 1.0

# Per-session bound on delivering one notification. The transport's write path
# can back-pressure when a client stops draining its GET stream; a wedged
# session must not hold up the broadcast to the others (they are sent
# concurrently anyway) or the notifier's next round.
SEND_TIMEOUT_SECONDS: float = 5.0


@dataclass
class _RunBox:
    """Per-`Server.run` slot for the session that run is serving. `run` sets a
    fresh box in a contextvar; request handlers (spawned from inside `run`, so
    they inherit the context) fill it in. Lets `run`'s `finally` drop exactly the
    session it served without touching SDK internals."""

    session: ServerSession | None = None


_current_run: contextvars.ContextVar[_RunBox | None] = contextvars.ContextVar(
    "snowline_gateway_current_run", default=None
)


@dataclass
class SessionSet:
    """The live sessions of one surface that have listed tools. Thread-safe;
    snapshot via `snapshot()` before iterating."""

    _sessions: set[ServerSession] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, session: ServerSession) -> None:
        with self._lock:
            self._sessions.add(session)

    def discard(self, session: ServerSession) -> None:
        with self._lock:
            self._sessions.discard(session)

    def snapshot(self) -> list[ServerSession]:
        with self._lock:
            return list(self._sessions)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


class ListChangedServer(Server):
    """A low-level `Server` that advertises ``tools.listChanged`` and tracks the
    sessions it serves.

    Tracking uses only public SDK surface: `run` (one call per session in
    stateful streamable-HTTP) opens a `_RunBox`; the list handler calls
    `track_current_session()`, which records `request_context.session` into the
    box and the `SessionSet`; `run`'s `finally` discards it — however the
    session ends (client DELETE, idle timeout, crash, shutdown). A STATELESS run
    (one throwaway session per request) opens no box, so nothing is tracked:
    there is no stream to notify on."""

    def __init__(self, name: str, sessions: SessionSet | None = None) -> None:
        super().__init__(name)
        self.sessions = sessions if sessions is not None else SessionSet()

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict | None = None,
    ) -> InitializationOptions:
        # The session manager calls this with no arguments, so the default is
        # what the client sees in `initialize`'s capabilities.
        return super().create_initialization_options(
            notification_options or NotificationOptions(tools_changed=True),
            experimental_capabilities,
        )

    async def run(
        self,
        read_stream,
        write_stream,
        initialization_options,
        raise_exceptions: bool = False,
        stateless: bool = False,
    ):
        if stateless:
            return await super().run(
                read_stream,
                write_stream,
                initialization_options,
                raise_exceptions=raise_exceptions,
                stateless=stateless,
            )
        box = _RunBox()
        token = _current_run.set(box)
        try:
            return await super().run(
                read_stream,
                write_stream,
                initialization_options,
                raise_exceptions=raise_exceptions,
                stateless=stateless,
            )
        finally:
            _current_run.reset(token)
            if box.session is not None:
                self.sessions.discard(box.session)

    def track_current_session(self) -> None:
        """Record the session of the request being handled as a notification
        target. Call from inside a request handler; a no-op outside a stateful
        `run` (stateless transport, or a direct in-process call)."""
        box = _current_run.get()
        if box is None or box.session is not None:
            return
        try:
            session = self.request_context.session
        except LookupError:
            return
        box.session = session
        self.sessions.add(session)


class SurfaceChangeNotifier:
    """Registry observer + debounced `tools/list_changed` broadcaster for ONE
    surface.

    `registry_changed` is the observer: it runs inline on whatever thread wrote
    the registry, so it does only cheap work — re-discover the surface's
    upstreams, compare with the last set seen, and if different mark dirty and
    wake the broadcaster (thread-safely, via the loop captured by `run`). It
    never raises into the registry and never blocks on the network.

    `run` is the broadcaster, entered for the gateway lifespan: wait for a wake,
    sleep `DEBOUNCE_SECONDS` to coalesce the burst, then — if still dirty —
    notify every tracked session concurrently. One session failing (closed
    stream → dropped from the set; anything else → logged) or stalling (bounded
    by `SEND_TIMEOUT_SECONDS`) does not affect the others."""

    def __init__(
        self,
        registry: PluginRegistry,
        surface: str,
        sessions: SessionSet,
        allowlist: frozenset[str] | None = None,
    ) -> None:
        self._registry = registry
        self._surface = surface
        self._allowlist = allowlist
        self.sessions = sessions
        self._lock = threading.Lock()
        self._dirty = False
        self._last: tuple[Upstream, ...] = self._discover()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: anyio.Event | None = None
        # Broadcast count — for tests and logs.
        self.broadcasts = 0

    @property
    def surface(self) -> str:
        return self._surface

    def _discover(self) -> tuple[Upstream, ...]:
        return tuple(
            discover_upstreams(self._registry, self._surface, self._allowlist)
        )

    def registry_changed(self, change: RegistryChange) -> None:
        current = self._discover()
        with self._lock:
            if current == self._last:
                return
            self._last = current
            self._dirty = True
            loop = self._loop
        log.debug(
            "gateway: surface %r upstreams changed (%s %r) — list_changed queued",
            self._surface,
            change.kind,
            change.name,
        )
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._set_wake)
            except RuntimeError:
                # Loop closed between the check and the call (shutdown race):
                # nobody is left to notify.
                pass

    def _set_wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def run(self) -> None:
        """Broadcaster loop; runs until cancelled (the gateway lifespan's task
        group cancels it on shutdown)."""
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._wake = anyio.Event()
            # Re-baseline at lifespan start: changes made before serving began
            # (create_app seeding) have no listening session to tell.
            self._last = self._discover()
            self._dirty = False
        try:
            while True:
                await self._wake.wait()
                self._wake = anyio.Event()
                await anyio.sleep(DEBOUNCE_SECONDS)
                with self._lock:
                    dirty, self._dirty = self._dirty, False
                if dirty:
                    await self.broadcast()
        finally:
            with self._lock:
                self._loop = None

    async def broadcast(self) -> None:
        """Send `tools/list_changed` to every tracked session of this surface."""
        targets = self.sessions.snapshot()
        self.broadcasts += 1
        if not targets:
            return
        log.info(
            "gateway: surface %r tool set changed — notifying %d session(s)",
            self._surface,
            len(targets),
        )
        async with anyio.create_task_group() as tg:
            for session in targets:
                tg.start_soon(self._send_one, session)

    async def _send_one(self, session: ServerSession) -> None:
        try:
            with anyio.move_on_after(SEND_TIMEOUT_SECONDS) as scope:
                await session.send_tool_list_changed()
            if scope.cancelled_caught:
                log.warning(
                    "gateway: list_changed to a session on surface %r timed "
                    "out after %.1fs; leaving it tracked",
                    self._surface,
                    SEND_TIMEOUT_SECONDS,
                )
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            # The session's transport is gone; its run() will discard it too,
            # but drop it now so the next broadcast doesn't retry a dead stream.
            self.sessions.discard(session)
        except Exception:
            log.exception(
                "gateway: list_changed to a session on surface %r failed",
                self._surface,
            )
