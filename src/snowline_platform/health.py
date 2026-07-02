"""The plugin HEALTH poller — sets the registry status the gateway routes on.

Plugins are out-of-process and URL-addressed (local OR cross-tailnet), so any can
crash or go unreachable on its own. This component is the half of health-aware
composition that *sets* `RegisteredPlugin.status`; the gateway already *consults*
it (`discover_upstreams` skips `DOWN`). Crashed-local and unreachable-remote are
treated identically (health.md): route-around, surface, keep retrying.

The signal is deliberately minimal — a 2xx from ``base_url + health_path`` within
the timeout is healthy; anything else (non-2xx, connection refused, DNS/TLS
error, timeout) is `DOWN`. There is no body contract; 2xx *is* the contract. A
`DOWN` plugin that recovers flips back to `UP` on the next round automatically.

A single background task (started in the app lifespan, cancelled on shutdown)
polls every registered plugin every `interval` seconds, each check bounded by
`timeout`, all plugins in a round concurrently so the round costs ~one timeout
rather than N. Results are written via `registry.set_status`, which is a no-op if
the plugin was unregistered mid-round — the poller never resurrects a removed
entry. One plugin's failure is just that plugin's `DOWN`; it never aborts the
round, and the loop swallows+logs unexpected errors so it outlives transient
faults.
"""

from __future__ import annotations

import logging

import anyio
import httpx

from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import (
    PluginRegistry,
    PluginStatus,
    RegisteredPlugin,
)

log = logging.getLogger("snowline_platform.health")


def health_url(manifest: PluginManifest) -> str:
    """The plugin's health endpoint: ``base_url + health_path`` (both manifest
    fields; `base_url` is already trailing-slash-trimmed, `health_path` defaults
    to ``/health``)."""
    return f"{manifest.base_url}{manifest.health_path}"


async def check(client: httpx.AsyncClient, entry: RegisteredPlugin) -> PluginStatus:
    """Poll one plugin's health endpoint → `UP` (2xx) or `DOWN` (anything else).

    Every transport failure — connection refused (crashed local), DNS/TLS error
    or timeout (unreachable remote) — is caught and mapped to `DOWN`, never
    raised: an unhealthy plugin must not break the round."""
    try:
        resp = await client.get(health_url(entry.manifest))
    except httpx.HTTPError as exc:
        log.info(
            "health: %s unreachable at %s (%s)",
            entry.manifest.name,
            health_url(entry.manifest),
            exc,
        )
        return PluginStatus.DOWN
    return PluginStatus.UP if resp.is_success else PluginStatus.DOWN


async def poll_once(
    registry: PluginRegistry, client: httpx.AsyncClient
) -> dict[str, PluginStatus]:
    """Poll every currently-registered plugin CONCURRENTLY and write each result
    back to the registry. Returns the name→status map polled this round (useful
    for tests/observability). `set_status` is a no-op for a plugin unregistered
    mid-round, so a concurrent unregister is safe."""
    results: dict[str, PluginStatus] = {}

    async def _one(entry: RegisteredPlugin) -> None:
        # Backstop: `check` already maps transport errors to DOWN, but a residual
        # non-HTTPError (e.g. a RuntimeError from a client closed mid-round on
        # shutdown) must stay THIS plugin's DOWN — never abort the task group and
        # cancel the sibling checks (round isolation, health.md).
        try:
            status = await check(client, entry)
        except Exception:
            log.exception("health: unexpected error checking %s", entry.manifest.name)
            status = PluginStatus.DOWN
        # `expected_entry` pins the write to the entry this round actually
        # probed: if an `updated` upsert replaced it mid-round, the result
        # describes the OLD address and must not mark the new entry (issue #39).
        registry.set_status(entry.manifest.name, status, expected_entry=entry)
        results[entry.manifest.name] = status

    async with anyio.create_task_group() as tg:
        for entry in registry.list():
            tg.start_soon(_one, entry)
    return results


async def health_poll_loop(
    registry: PluginRegistry,
    *,
    interval: float,
    timeout: float,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Poll all plugins every `interval` seconds until cancelled.

    Owns an `httpx.AsyncClient` (per-request `timeout`, redirects followed) unless
    one is injected (tests). Each round is a `poll_once`; an unexpected error in a
    round is logged and the loop continues — the poller must outlive transient
    faults. Cancellation (lifespan shutdown) unwinds cleanly through `anyio`."""
    own_client = client is None
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(timeout), follow_redirects=True
    )
    empty_rounds = 0
    try:
        while True:
            try:
                results = await poll_once(registry, client)
                # The hollow-gateway detector (issue #39): right after a
                # platform boot an empty registry is expected for ~one
                # registration-heartbeat interval, but if it STAYS empty the
                # plugins' heartbeats aren't arriving (wrong platform URL,
                # plugins down) and every composed surface serves zero tools.
                # Warn once when emptiness persists past the first round; go
                # quiet until plugins appear so the log isn't flooded.
                if results:
                    empty_rounds = 0
                else:
                    empty_rounds += 1
                    if empty_rounds == 2:
                        log.warning(
                            "health: registry still empty after %d poll rounds "
                            "— no plugin registration heartbeats are arriving; "
                            "composed surfaces serve no tools (issue #39)",
                            empty_rounds,
                        )
            except Exception:  # never let one bad round kill the poller
                log.exception("health: poll round failed; continuing")
            await anyio.sleep(interval)
    finally:
        # Shutdown cancels this task mid-sleep/mid-poll, so the finally runs under
        # an already-cancelled scope; shield the teardown so the owned client's
        # connections actually close instead of being re-cancelled (no 'Unclosed
        # client' leak).
        if own_client:
            with anyio.CancelScope(shield=True):
                await client.aclose()
