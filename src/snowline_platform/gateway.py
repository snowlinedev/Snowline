"""The platform GATEWAY — composes registered plugins into named MCP surfaces.

This is what makes Snowline a *platform* (gateway.md, architecture §"surface
composition", decision-derived model 70b415fd): the platform exposes **named MCP
surfaces** (`main`, `shadow`, …); each plugin's manifest **maps its own surfaces**
onto them (governance: ``{"/mcp": "main", "/shadow/mcp": "shadow"}``); and for
each named surface the gateway **aggregates** every plugin-surface mapped to it
into the one MCP endpoint the client connects to.

The gateway never imports plugin code — it reads the in-memory `PluginRegistry`
and talks to each plugin only over MCP/streamable-HTTP at ``base_url + plugin``
(local OR cross-tailnet). Composition is the whole job:

  - **Discovery** — a named surface's upstreams = every registered plugin whose
    ``manifest.surfaces`` maps one of its plugin-paths to that surface. A plugin
    whose registry ``status`` is ``DOWN`` is SKIPPED (health-aware route-around,
    §4); ``UNKNOWN`` is treated as routable (the health poller that sets ``UP``
    is a later PR). A surface with an explicit plugin ALLOWLIST additionally
    PROJECTS each allowlisted plugin's `ROOT_SURFACE` mapping when the plugin
    has no native mapping for that surface (issue #38 — see
    `discover_upstreams`).
  - **list_tools** — the merged union of the upstreams' tool lists, with each
    tool NAMESPACED by its owning plugin (``governance.record_decision``). See
    `namespaced_name` for the collision policy.
  - **call_tool** — un-namespace ``plugin.tool`` → route to that plugin's session
    and return the upstream `CallToolResult` verbatim (content + structured +
    isError preserved).
  - **Isolation is plugin-side and structural** — the gateway does NO per-tool
    filtering. A tool appears on a named surface ONLY because a plugin mapped a
    surface carrying it onto that named surface; ``record_decision`` lands on
    `main` and is absent from `shadow` purely by composition (decision 8a7f0a11).

Connections are PER-REQUEST (open `ClientSession`, list/call, close). `list_tools`
fans the per-upstream connect+list out CONCURRENTLY with a bounded per-upstream
timeout (issue #23) so a surface with N plugins costs ~one round-trip window and a
slow upstream can't stall the surface. Connection POOLING/caching of upstream
sessions is still a deferred perf follow-up (gateway.md pragmatics) — the
per-request connect+initialize handshake remains; pooling was left out to keep
this change focused on the concurrency+timeout win. The upstream connection is
abstracted behind `UpstreamConnector` so tests can wire an in-memory MCP plugin
without standing up HTTP, while production uses the streamable-HTTP client.

**Connect-phase retry (issue #58, deploy-continuity.md §3 — layer 2):** a plugin
kickstart (deploy/restart) makes the per-request upstream connect fail for the
sub-second-to-few-seconds the plugin process is down. The gateway retries ONLY
the connect+initialize phase — the safety line is that a tool call is not
idempotent in general, so nothing past the point `call_tool` is written to the
wire may ever be retried. See `_retry_transient` + its two call sites
(`SurfaceGateway.call_tool` retries only the connect step; `list_tools` retries
the whole read since it's idempotent) for the structural boundary.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol, TypeVar

import anyio
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server

from snowline_platform.registry import PluginRegistry, PluginStatus

log = logging.getLogger("snowline_platform.gateway")

_T = TypeVar("_T")

# Composed server INSTRUCTIONS (issue #251). Each plugin's MCP `instructions`
# (its cross-tool conventions, injected into the client's context at session
# start) is surfaced under a per-plugin heading in the surface's own
# `initialize` result. Caps are in UTF-8 BYTES and bound what a client's context
# pays: per plugin, then overall; anything cut is replaced by a visible marker
# and logged at WARNING.
INSTRUCTIONS_PER_PLUGIN_MAX = 4 * 1024
INSTRUCTIONS_TOTAL_MAX = 16 * 1024
INSTRUCTIONS_TRUNCATION_MARKER = "\n[... truncated by gateway ...]"
# A successful fetch is re-used until the plugin's registration changes
# (observer below) OR this long, whichever first — the TTL only covers a plugin
# redeploy that kept its manifest identical (the 15s heartbeat is a no-op).
INSTRUCTIONS_TTL = 600.0
# A plugin that failed to answer is not re-tried on every initialize.
INSTRUCTIONS_FAILURE_TTL = 30.0

# Connect-phase retry policy (issue #58 / deploy-continuity.md §3). Module-level
# so tests can `monkeypatch.setattr` it down to near-zero for fast retry tests.
# Length is load-bearing: attempts made = len(CONNECT_RETRY_BACKOFFS) + 1 (the
# first try plus one retry per backoff) — "2 retries, ~250ms then ~750ms" is
# expressed as the tuple itself, not a separate retry-count constant, so the two
# can never drift apart.
CONNECT_RETRY_BACKOFFS: tuple[float, ...] = (0.25, 0.75)

# The root surface: the composed daily-driver surface, served at the bare
# ``/mcp``. Defined HERE (the bottom of the gateway import graph) because the
# gateway itself needs it twice — the manifest surface-map default
# (``{mcp_path: ROOT_SURFACE}``) and allowlist PROJECTION (issue #38, below) —
# and `gateway_app` (which routes it to ``/mcp``) already imports from this
# module, so defining it there would be an import cycle. `gateway_app` and
# `config.surfaces()` re-use THIS constant, so the assumption still lives in
# one place.
ROOT_SURFACE = "main"

# How a namespaced gateway tool name (``plugin__tool``) is split. We delimit with
# a DOUBLE UNDERSCORE — the ``mcp__<server>__<tool>`` ecosystem convention — for a
# load-bearing reason: the gateway's tools are consumed by Claude (the integration
# runtime, decision 61e50214), and the Anthropic tool-name charset is
# ``^[a-zA-Z0-9_-]{1,64}$`` — a DOT is rejected there even though the MCP
# server-side regex (``^[A-Za-z0-9._-]{1,128}$``) permits it. So a dot separator
# passes every in-repo test yet breaks the one real consumer. ``__`` is in-charset
# and unambiguous: plugin names are url-safe slugs (``[a-z0-9][a-z0-9-]*`` — no
# underscores at all), so ``__`` never occurs inside a plugin name and the FIRST
# ``__`` cleanly separates the plugin prefix from the upstream tool name (which may
# itself contain ``_`` or even ``__``; we split on the first to stay robust).
_NS_SEP = "__"


def namespaced_name(plugin_name: str, tool_name: str) -> str:
    """The gateway-visible name for an upstream tool.

    **Collision policy = namespace-by-plugin** (gateway.md §7 "Open"): the
    gateway prefixes every upstream tool with ``<plugin>__``. Two plugins on the
    same named surface that both expose ``record_decision`` therefore appear as
    ``governance__record_decision`` and ``other__record_decision`` — no
    collision, no silent shadowing, and the prefix is the routing key (call_tool
    reverses it). This is the safer of the two options the spec floated (vs
    first-wins-with-warning), and it is unambiguous because plugin names are
    url-safe slugs containing no underscore. The ``__`` delimiter keeps the name
    inside the Anthropic tool-name charset so Claude — the consumer — accepts it.
    """
    return f"{plugin_name}{_NS_SEP}{tool_name}"


def split_namespaced(name: str) -> tuple[str, str]:
    """Reverse `namespaced_name`: ``plugin__tool`` → ``(plugin, tool)``.

    Splits on the FIRST ``__``, so an upstream tool name that itself contained a
    ``__`` would survive in the suffix. Raises `ValueError` for an un-namespaced
    name (no ``__``) — that is a routing bug, surfaced as a clear call_tool error
    rather than a silent misroute."""
    plugin, sep, tool = name.partition(_NS_SEP)
    if not sep or not plugin or not tool:
        raise ValueError(
            f"tool name {name!r} is not gateway-namespaced (expected "
            f"'<plugin>{_NS_SEP}<tool>')"
        )
    return plugin, tool


@dataclass(frozen=True)
class Upstream:
    """One plugin-surface mapped onto a named platform surface — the address the
    gateway connects to. `plugin_name` is the namespace prefix + routing key;
    `base_url` + `plugin_path` is the streamable-HTTP endpoint."""

    plugin_name: str
    base_url: str
    plugin_path: str

    @property
    def url(self) -> str:
        return f"{self.base_url}{self.plugin_path}"


# An UpstreamConnector.connect() yields a *connected, initialized* ClientSession
# for the duration of an `async with`, then tears it down. Per-request: one
# connect per list/call. Abstracted so tests can inject an in-memory transport.
class AbstractAsyncCM(Protocol):
    async def __aenter__(self): ...
    async def __aexit__(self, *exc) -> bool | None: ...


class InstructionsSession(ClientSession):
    """A `ClientSession` that remembers the upstream's `initialize` result
    instructions (`ClientSession.initialize()` returns them but the connector
    swallows the call) so the gateway can compose them (issue #251). Connectors
    must yield this class for a plugin's instructions to be composed; a session
    without ``server_instructions`` contributes none."""

    server_instructions: str | None = None

    async def initialize(self) -> types.InitializeResult:
        result = await super().initialize()
        self.server_instructions = result.instructions
        return result


class UpstreamConnector(Protocol):
    """Opens a `ClientSession` to one upstream plugin-surface.

    Production: `StreamableHttpConnector` (HTTP to ``base_url + plugin_path``).
    Tests: an in-memory connector wired to a real `FastMCP`/`Server` so the
    aggregation + routing path is exercised without standing up HTTP."""

    def connect(self, upstream: Upstream) -> AbstractAsyncCM[ClientSession]: ...


class StreamableHttpConnector:
    """Production connector: streamable-HTTP to ``base_url + plugin_path``.

    Opens a fresh `streamablehttp_client` transport + `ClientSession` per call
    and initializes it. The streaming + session semantics live entirely in the
    mcp client/transport: we open the transport, run one request (list or call)
    on the initialized session, and the upstream's response — including a
    streamed/chunked tool result — is reassembled by `ClientSession` into the
    `ListToolsResult` / `CallToolResult` we return. Per-request connect keeps
    the first cut correct; pooling is a deferred perf follow-up."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    @asynccontextmanager
    async def connect(self, upstream: Upstream) -> AsyncIterator[ClientSession]:
        import httpx

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout), follow_redirects=True
        ) as http_client:
            async with streamable_http_client(
                upstream.url, http_client=http_client
            ) as (read, write, _get_session_id):
                async with InstructionsSession(read, write) as session:
                    await session.initialize()
                    yield session


class GatewayError(Exception):
    """A composition/routing failure the gateway surfaces as a clear MCP error
    rather than hanging (e.g. a tool routed to a plugin not on this surface, or
    a down/unreachable upstream)."""


async def _retry_transient(
    attempt: Callable[[], Awaitable[_T]],
    *,
    upstream: Upstream,
    surface: str,
    what: str,
) -> _T:
    """Call `attempt()` up to ``len(CONNECT_RETRY_BACKOFFS) + 1`` times total,
    sleeping the next `CONNECT_RETRY_BACKOFFS` entry between attempts, on ANY
    exception (issue #58 / deploy-continuity.md §3). Re-raises the last
    exception once retries are exhausted — the CALLER decides what that means
    (a WARNING + `GatewayError` for `call_tool`'s connect step; a route-around
    for `list_tools`'s whole read) because the two sites have different
    failure semantics, not because retrying itself differs.

    This is deliberately NOT exception-type-aware: whether a failure is safe to
    retry is decided STRUCTURALLY by what `attempt` closes over, not by
    inspecting the exception. `SurfaceGateway.call_tool` passes an `attempt`
    that does ONLY the connect+initialize (so only that step is ever retried,
    and a `call_tool` write — issued outside this function, exactly once — is
    never re-run). `list_tools` passes an `attempt` that does connect+list
    together, because the whole thing is a read and safe to retry in full.
    Cancellation (`BaseException`, not `Exception` — e.g. a `fail_after`
    timeout firing) is never caught here and propagates immediately, so a
    bounding deadline around a retry loop still cuts it off promptly."""
    for i in range(len(CONNECT_RETRY_BACKOFFS) + 1):
        try:
            return await attempt()
        except Exception as exc:
            if i >= len(CONNECT_RETRY_BACKOFFS):
                raise
            backoff = CONNECT_RETRY_BACKOFFS[i]
            log.debug(
                "gateway: %s to upstream %s (plugin %r) on surface %r failed "
                "(attempt %d/%d), retrying in %.2fs: %s",
                what,
                upstream.url,
                upstream.plugin_name,
                surface,
                i + 1,
                len(CONNECT_RETRY_BACKOFFS),
                backoff,
                exc,
            )
            await anyio.sleep(backoff)
    raise AssertionError("unreachable")  # pragma: no cover


def discover_upstreams(
    registry: PluginRegistry,
    surface: str,
    allowlist: frozenset[str] | None = None,
) -> list[Upstream]:
    """Every registered plugin-surface mapped onto the named `surface`, SKIPPING
    plugins whose status is `DOWN` (health-aware route-around, gateway.md §4).

    A plugin's effective surface map is `manifest.surfaces`, defaulting (per the
    manifest contract) to ``{mcp_path: "main"}`` when empty — so a plugin that
    declares no surfaces still composes onto `main`. `UNKNOWN` is routable (the
    poller that promotes to `UP` is a later PR); only an explicit `DOWN` is
    skipped, so the gateway route-arounds a dead upstream instead of hanging on
    it. Ordered by plugin name for a stable merged tool list.

    **Duplicate-plugin guard (issue #22, policy (a) "reject the duplicate"):** a
    plugin that maps TWO distinct plugin-paths onto the SAME named surface (e.g.
    ``{"/mcp": "main", "/admin": "main"}``) would have both paths' tools
    aggregated by `list_tools` but namespaced IDENTICALLY (``<plugin>__<tool>`` —
    the namespace is keyed by plugin name only, not plugin+path), and `call_tool`
    builds its routing map keyed by `plugin_name`, so only ONE path is reachable —
    the other path's tools silently misroute. Because the namespace itself can't
    distinguish the two paths, there is no safe routable form under the v1
    name-by-plugin model, so we REJECT the extra path(s): keep the
    lexicographically-first plugin_path for that ``(plugin, surface)`` pair, drop
    the rest, and `log.warning` it as a configuration error. This keeps
    `list_tools` from advertising unroutable tools (it never sees the dropped
    path) and matches the call_tool routing key, so the two stay consistent. The
    cleaner fix — namespacing by plugin+path — is deferred until a plugin actually
    needs multi-path-per-surface; today none do.

    **Per-surface plugin allowlist (issue #36):** `allowlist` is THIS surface's
    plugin allowlist — ``None`` means no allowlist (allow every plugin, the
    default, backward-compatible behavior); a `frozenset` restricts which
    plugins the surface aggregates (e.g. a governance-only `core` surface
    without the private PM plugin). A plugin whose name is NOT in the allowlist
    is skipped here — at the aggregation step, so it is absent from BOTH
    `list_tools` and `call_tool` routing (a filtered plugin's tool is
    unroutable, surfaced as the same clear error as any not-on-this-surface
    tool). The allowlist is parsed + validated ONCE at mount time
    (`gateway_app.build_surface_mounts` ← `config.surface_plugins()`, fail-loud
    at boot) and handed down frozen — there is no per-request env re-parse.
    Registration/health/registry views are untouched — this filters aggregation
    only.

    **Projection onto allowlisted surfaces (issue #38):** an allowlist is an
    operator statement of COMPOSITION ("`core` = governance's tools, without
    PM"), but no real plugin's manifest maps anything onto an operator-invented
    surface name (governance maps only ``/mcp → main`` + ``/shadow/mcp →
    shadow``), so a pure filter over manifest mappings served an EMPTY surface
    — found live, minutes after #37 deployed. So an allowlisted surface
    COMPOSES, per allowlisted plugin: the plugin's NATIVE mapping for this
    surface when its manifest declares one, ELSE its `ROOT_SURFACE` (``main``)
    mapping — no plugin-side manifest change needed. A native mapping WINS
    outright (the main mapping is not also projected, so projection can never
    trip the issue-#22 duplicate-path guard by itself); an allowlisted plugin
    with neither mapping contributes nothing. A surface WITHOUT an allowlist
    (``None``) keeps pure manifest-driven semantics, unchanged — projection is
    strictly a property of the explicit allowlist, so ``main`` tools can never
    leak onto e.g. ``shadow``."""
    upstreams: list[Upstream] = []
    # Track the path already accepted for each (plugin, surface) so a second path
    # onto the same surface is rejected with a warning (issue #22). We sort each
    # manifest's surface map by path first so the KEPT path is deterministic
    # (lexicographically-first) regardless of dict insertion order.
    accepted_path: dict[str, str] = {}
    for entry in registry.list():
        if allowlist is not None and entry.manifest.name not in allowlist:
            # Filtered out by this surface's plugin allowlist (issue #36).
            continue
        if entry.status is PluginStatus.DOWN:
            log.info(
                "gateway: skipping down plugin %r for surface %r",
                entry.manifest.name,
                surface,
            )
            continue
        manifest = entry.manifest
        surface_map = manifest.surfaces or {manifest.mcp_path: ROOT_SURFACE}
        # Projection (issue #38): on an ALLOWLISTED surface, a plugin with no
        # native mapping for this surface contributes its ROOT_SURFACE mapping
        # instead. `target` stays `surface` whenever a native mapping exists
        # (native wins — the main mapping is never ALSO iterated, so projection
        # cannot introduce a duplicate path for the #22 guard) and always for
        # allowlist-less surfaces (pure manifest-driven semantics, unchanged).
        target = surface
        if allowlist is not None and surface not in surface_map.values():
            target = ROOT_SURFACE
        for plugin_path in sorted(surface_map):
            if surface_map[plugin_path] != target:
                continue
            kept = accepted_path.get(manifest.name)
            if kept is not None:
                log.warning(
                    "gateway: plugin %r maps multiple paths (%r and %r) onto "
                    "surface %r — rejecting %r as a config error; its tools "
                    "would be unroutable (namespace is keyed by plugin only). "
                    "Map one path per surface, or split into distinct plugins.",
                    manifest.name,
                    kept,
                    plugin_path,
                    surface,
                    plugin_path,
                )
                continue
            accepted_path[manifest.name] = plugin_path
            upstreams.append(
                Upstream(
                    plugin_name=manifest.name,
                    base_url=manifest.base_url,
                    plugin_path=plugin_path,
                )
            )
    upstreams.sort(key=lambda u: (u.plugin_name, u.plugin_path))
    return upstreams


@dataclass
class _CachedInstructions:
    """One plugin's cached instructions: `text` is None for "no instructions" OR
    a failed fetch (`failed`), valid until `expires`."""

    upstream: Upstream
    text: str | None
    expires: float
    failed: bool = False


def _cap_instructions(text: str, max_bytes: int) -> tuple[str, bool]:
    """`text` bounded to `max_bytes` UTF-8 bytes INCLUDING the visible
    truncation marker (cut on a character boundary). Returns
    ``(text, was_truncated)``."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    marker_bytes = len(INSTRUCTIONS_TRUNCATION_MARKER.encode("utf-8"))
    cut = raw[: max(0, max_bytes - marker_bytes)].decode("utf-8", errors="ignore")
    return cut + INSTRUCTIONS_TRUNCATION_MARKER, True


class SurfaceGateway:
    """Aggregates the upstreams of ONE named surface into list/call handlers.

    Stateless beyond its `registry` + `connector` + `surface` name + frozen
    `allowlist`: it re-discovers upstreams on every `list_tools`/`call_tool`, so
    a plugin registered/unregistered/marked-down at runtime is reflected on the
    next request without a restart (hot-plug, architecture §3). Discovery is
    live, so the health-aware route-around is too. The plugin ALLOWLIST is the
    one deliberately-static input: parsed + validated once at boot
    (`build_surface_mounts`) and passed in frozen, so a config change is a
    restart — never a mid-run reinterpretation (issue #36 review)."""

    # Per-upstream connect+list_tools budget (issue #23), SEPARATE from the
    # connector's 30s call timeout. `list_tools` fans out concurrently, so this
    # bounds the WHOLE surface's list latency to one such window even if an
    # upstream is UNKNOWN-but-wedged (a slow upstream, not yet marked DOWN by the
    # poller): it's route-around-ed on timeout instead of stalling the surface.
    LIST_TIMEOUT: float = 5.0

    def __init__(
        self,
        registry: PluginRegistry,
        surface: str,
        connector: UpstreamConnector,
        allowlist: frozenset[str] | None = None,
    ) -> None:
        self._registry = registry
        self._surface = surface
        self._connector = connector
        self._allowlist = allowlist
        # Composed-instructions state (issue #251). `composed_instructions` is
        # read SYNCHRONOUSLY by the server's initialization options, so it is
        # filled asynchronously beforehand by `refresh_instructions`.
        self._instructions: dict[str, _CachedInstructions] = {}
        self._instructions_lock = anyio.Lock()
        self._composed: str | None = None
        self._composed_key: tuple | None = None
        # A registration / manifest change drops that plugin's cached entry; a
        # no-op heartbeat re-register emits nothing (registry.upsert).
        registry.add_observer(self._on_registry_change)

    @property
    def surface(self) -> str:
        return self._surface

    def _on_registry_change(self, change) -> None:
        self._instructions.pop(change.name, None)

    @property
    def composed_instructions(self) -> str | None:
        """The current composed instructions (None = none). Cheap and sync."""
        return self._composed

    async def refresh_instructions(self) -> None:
        """Bring the composed instructions up to date. Only upstreams with no
        fresh cache entry (new, re-registered, expired, moved) are contacted --
        concurrently, with `LIST_TIMEOUT` and connect retry like `list_tools`;
        a failing one is logged and skipped. Sections are ordered by plugin
        name. Never raises."""
        try:
            async with self._instructions_lock:
                await self._refresh_instructions()
        except Exception:  # must never fail an initialize
            log.exception(
                "gateway: composing instructions for surface %r failed",
                self._surface,
            )

    async def _refresh_instructions(self) -> None:
        upstreams = discover_upstreams(
            self._registry, self._surface, self._allowlist
        )
        live = {u.plugin_name for u in upstreams}
        for name in [n for n in self._instructions if n not in live]:
            del self._instructions[name]
        now = time.monotonic()
        stale = [
            u
            for u in upstreams
            if (c := self._instructions.get(u.plugin_name)) is None
            or c.upstream != u
            or c.expires <= now
        ]

        async def _fetch(upstream: Upstream) -> None:
            async def _attempt() -> str | None:
                async with self._connector.connect(upstream) as session:
                    return getattr(session, "server_instructions", None)

            try:
                with anyio.fail_after(self.LIST_TIMEOUT):
                    text = await _retry_transient(
                        _attempt,
                        upstream=upstream,
                        surface=self._surface,
                        what="instructions",
                    )
            except Exception as exc:
                log.warning(
                    "gateway: instructions fetch failed for upstream %s on "
                    "surface %r (skipping): %s",
                    upstream.url,
                    self._surface,
                    exc,
                )
                self._instructions[upstream.plugin_name] = _CachedInstructions(
                    upstream,
                    None,
                    time.monotonic() + INSTRUCTIONS_FAILURE_TTL,
                    True,
                )
                return
            text = (text or "").strip() or None
            if text is not None:
                text, cut = _cap_instructions(text, INSTRUCTIONS_PER_PLUGIN_MAX)
                if cut:
                    log.warning(
                        "gateway: instructions of plugin %r truncated to %d "
                        "bytes on surface %r",
                        upstream.plugin_name,
                        INSTRUCTIONS_PER_PLUGIN_MAX,
                        self._surface,
                    )
            self._instructions[upstream.plugin_name] = _CachedInstructions(
                upstream, text, time.monotonic() + INSTRUCTIONS_TTL
            )

        if stale:
            async with anyio.create_task_group() as tg:
                for upstream in stale:
                    tg.start_soon(_fetch, upstream)

        sections = [
            (name, c.text)
            for name in sorted(live)
            if (c := self._instructions.get(name)) is not None and c.text
        ]
        key = tuple(sections)
        if key != self._composed_key:
            self._composed_key = key
            self._composed = self._compose(sections)

    def _compose(self, sections: list[tuple[str, str]]) -> str | None:
        """One section per plugin (``## <plugin>``) in name order, bounded to
        `INSTRUCTIONS_TOTAL_MAX` bytes: the section that overflows is cut at a
        visible marker and later plugins are dropped (logged at WARNING)."""
        parts: list[str] = []
        used = 0
        sep = "\n\n"
        for i, (name, text) in enumerate(sections):
            part = f"## {name}\n{text}"
            lead = len(sep) if parts else 0
            cost = len(part.encode("utf-8")) + lead
            if used + cost <= INSTRUCTIONS_TOTAL_MAX:
                parts.append(part)
                used += cost
                continue
            room = INSTRUCTIONS_TOTAL_MAX - used - lead
            omitted = [n for n, _ in sections[i + 1 :]]
            header = f"## {name}\n"
            if room > len(header) + len(INSTRUCTIONS_TRUNCATION_MARKER):
                part, _ = _cap_instructions(part, room)
                parts.append(part)
            else:
                omitted.insert(0, name)
                if parts:
                    parts[-1] += INSTRUCTIONS_TRUNCATION_MARKER
                else:
                    parts.append(INSTRUCTIONS_TRUNCATION_MARKER.strip())
            log.warning(
                "gateway: composed instructions for surface %r exceed %d "
                "bytes; truncated at plugin %r (omitted: %s)",
                self._surface,
                INSTRUCTIONS_TOTAL_MAX,
                name,
                ", ".join(omitted) or "none",
            )
            break
        return sep.join(parts) or None

    async def list_tools(self) -> list[types.Tool]:
        """Merged union of the upstreams' tool lists, each tool NAMESPACED by its
        owning plugin.

        The per-upstream connect+`tools/list` runs CONCURRENTLY (issue #23): a
        surface with N plugins costs ~one round-trip window, not ~N×T. Each
        upstream is fetched in its own task with its own try/except + a bounded
        `LIST_TIMEOUT`, so a failing OR slow upstream is logged and skipped
        (route-around) without failing or stalling the whole list — one dead/wedged
        plugin doesn't blank the surface. Results are merged in the upstreams'
        discovery order (already plugin-name-sorted) by collecting per-upstream
        slices and concatenating them by index, so the merged order is STABLE and
        independent of task-completion order.

        `list_tools` is read-only, so — unlike `call_tool` — connect-phase retry
        (issue #58) covers the WHOLE per-upstream attempt (connect+initialize AND
        the `tools/list` read itself): `_retry_transient` wraps `_attempt` below
        in full, still bounded overall by `LIST_TIMEOUT`."""
        upstreams = discover_upstreams(
            self._registry, self._surface, self._allowlist
        )
        # One result slot per upstream, filled by its task; concatenated in
        # discovery (name-sorted) order so the merged list is deterministic
        # regardless of which task finishes first.
        slices: list[list[types.Tool]] = [[] for _ in upstreams]

        async def _fetch(index: int, upstream: Upstream) -> None:
            async def _attempt() -> types.ListToolsResult:
                async with self._connector.connect(upstream) as session:
                    return await session.list_tools()

            try:
                with anyio.fail_after(self.LIST_TIMEOUT):
                    result = await _retry_transient(
                        _attempt,
                        upstream=upstream,
                        surface=self._surface,
                        what="list_tools",
                    )
            except Exception as exc:
                # Route-around a failing OR slow upstream, after retries (above)
                # were exhausted. `fail_after` converts its scope's timeout into
                # a plain `TimeoutError` on exit (caught here), so a slow
                # upstream is dropped after LIST_TIMEOUT rather than stalling
                # the surface. We deliberately do NOT catch the cancelled-exc
                # class: a real cancellation of the parent task group (lifespan
                # shutdown) must propagate, not be swallowed as a per-upstream
                # failure — `_retry_transient` only catches `Exception`, so a
                # cancellation firing mid-retry still passes straight through.
                # Dropping THIS upstream never cancels its siblings — each runs
                # in its own task.
                log.warning(
                    "gateway: list_tools failed for upstream %s on surface %r: "
                    "%s",
                    upstream.url,
                    self._surface,
                    exc,
                )
                return
            slices[index] = [
                _namespace_tool(upstream.plugin_name, tool)
                for tool in result.tools
            ]

        async with anyio.create_task_group() as tg:
            for index, upstream in enumerate(upstreams):
                tg.start_soon(_fetch, index, upstream)

        merged: list[types.Tool] = []
        for s in slices:
            merged.extend(s)
        return merged

    async def call_tool(
        self, name: str, arguments: dict | None
    ) -> types.CallToolResult:
        """Route a ``plugin.tool`` call to its owning upstream and return the
        upstream `CallToolResult` verbatim (content + structuredContent + isError
        preserved). The owning plugin is resolved from the namespace prefix and
        must be a live (non-down) upstream of THIS surface — so a tool the plugin
        only mapped onto another surface (e.g. `record_decision` on `main`) is
        unroutable here, which is the isolation property surfaced as an error.

        **Connect-phase retry (issue #58) — the safety line is STRUCTURAL, not
        exception-type-based.** The `_retry_transient` call below retries ONLY
        `stack.enter_async_context(self._connector.connect(upstream))` — i.e.
        opening + initializing the per-request upstream session. Once that
        returns a `session`, `session.call_tool(...)` is issued exactly ONCE,
        outside the retry, and its exceptions propagate un-retried: a tool call
        is not idempotent in general, so nothing past the point the call was
        written to the wire may ever be replayed. `AsyncExitStack` (rather than
        a plain `async with`) is what makes the boundary expressible this way —
        a fresh `connector.connect(upstream)` context manager is created and
        entered on `stack` per retry attempt (one that raised before yielding is
        exhausted and can't be re-entered), and once one succeeds its teardown
        is deferred to the `stack`'s own exit, which still runs whether
        `call_tool` below returns normally or raises."""
        plugin_name, tool_name = split_namespaced(name)
        upstreams = {
            u.plugin_name: u
            for u in discover_upstreams(
                self._registry, self._surface, self._allowlist
            )
        }
        upstream = upstreams.get(plugin_name)
        if upstream is None:
            raise GatewayError(
                f"no live plugin {plugin_name!r} on surface {self._surface!r} "
                f"for tool {name!r} (unregistered, down, or not mapped here)"
            )

        async with AsyncExitStack() as stack:
            try:
                session = await _retry_transient(
                    lambda: stack.enter_async_context(
                        self._connector.connect(upstream)
                    ),
                    upstream=upstream,
                    surface=self._surface,
                    what="connect",
                )
            except Exception as exc:
                log.warning(
                    "gateway: call_tool %r — upstream %s (plugin %r) on "
                    "surface %r unreachable after %d connect retries, giving "
                    "up: %s",
                    name,
                    upstream.url,
                    plugin_name,
                    self._surface,
                    len(CONNECT_RETRY_BACKOFFS),
                    exc,
                )
                raise GatewayError(
                    f"upstream {plugin_name!r} on surface {self._surface!r} "
                    f"unreachable for tool {name!r}: {exc}"
                ) from exc

            # Exactly one call_tool — no retry past this line (see docstring).
            return await session.call_tool(tool_name, arguments or {})


def _namespace_tool(plugin_name: str, tool: types.Tool) -> types.Tool:
    """A copy of `tool` with its name namespaced by `plugin_name`; description,
    schema, and annotations are carried verbatim (the ``<plugin>__`` prefix on the
    name is what keeps two same-named tools distinct + legible in the merged
    list)."""
    return tool.model_copy(
        update={"name": namespaced_name(plugin_name, tool.name)}
    )


class GatewayServer(Server):
    """A low-level `Server` whose `instructions` are the gateway's composed
    upstream instructions (issue #251). The SDK reads `instructions` in the
    SYNC `create_initialization_options()` (per connection when stateless), so
    the value is a live read of the gateway's cache -- filled beforehand by
    `SurfaceGateway.refresh_instructions` in the async layer (`gateway_app`)."""

    def __init__(self, name: str, gateway: SurfaceGateway) -> None:
        super().__init__(name)
        self.gateway = gateway

    @property
    def instructions(self) -> str | None:  # type: ignore[override]
        gateway = self.__dict__.get("gateway")
        return gateway.composed_instructions if gateway is not None else None

    @instructions.setter
    def instructions(self, value: str | None) -> None:
        pass  # Server.__init__ assigns None; the composition is the only source


def build_surface_server(
    registry: PluginRegistry,
    surface: str,
    connector: UpstreamConnector,
    allowlist: frozenset[str] | None = None,
    *,
    before_list: Callable[[], Awaitable[None]] | None = None,
    before_call: Callable[[str], Awaitable[None]] | None = None,
) -> Server:
    """A low-level `mcp.server.lowlevel.Server` for one named surface, wired to a
    `SurfaceGateway`'s list/call handlers.

    `before_list` is awaited at the top of every CLIENT `tools/list` — the
    post-restart startup grace hook (issue #240, `registry_state.StartupGrace.
    wait`); it must be bounded. It is deliberately skipped for the SDK's
    internal tool-cache refresh (the low-level server re-runs the list handler
    with ``req=None`` on a `call_tool` cache miss), so a call never waits on the
    whole surface. `before_call(name)` is awaited before routing each
    `tools/call` instead — the grace's per-call hook, which waits only for the
    plugin that owns `name`.

    We use the LOW-LEVEL Server (not FastMCP) deliberately: the gateway has no
    statically-known tool set — its tools are whatever the live upstreams expose
    — so a dynamic `list_tools` handler + a pass-through `call_tool` is exactly
    the shape, and the low-level server lets `call_tool` return an upstream
    `CallToolResult` UNMODIFIED (FastMCP would re-wrap content/structured). The
    Server is served over streamable-HTTP by a `StreamableHTTPSessionManager`
    mounted on the platform app (see `gateway_app`)."""
    gateway = SurfaceGateway(registry, surface, connector, allowlist)
    server: Server = GatewayServer(f"snowline-{surface}", gateway)

    # The positional-only, exactly-typed `req` makes the SDK pass the request
    # through (`create_call_wrapper`): a real client list carries a request, the
    # SDK's call_tool cache refresh passes None — only the former waits.
    @server.list_tools()
    async def _list_tools(req: types.ListToolsRequest, /) -> list[types.Tool]:
        if before_list is not None and req is not None:
            await before_list()
        return await gateway.list_tools()

    # validate_input=False: the OWNING plugin validates against its own
    # inputSchema; the gateway is a pass-through and must not double-validate
    # (and can't, without re-deriving the upstream schema per call). Returning
    # the upstream CallToolResult directly preserves isError + structuredContent.
    @server.call_tool(validate_input=False)
    async def _call_tool(
        name: str, arguments: dict
    ) -> types.CallToolResult:
        if before_call is not None:
            await before_call(name)
        try:
            return await gateway.call_tool(name, arguments)
        except GatewayError as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))],
                isError=True,
            )

    return server


# The handler factory is also exposed for tests that drive the Server directly.
__all__ = [
    "ROOT_SURFACE",
    "Upstream",
    "UpstreamConnector",
    "StreamableHttpConnector",
    "SurfaceGateway",
    "GatewayServer",
    "InstructionsSession",
    "GatewayError",
    "discover_upstreams",
    "build_surface_server",
    "namespaced_name",
    "split_namespaced",
]
