"""Registering the memory plugin with the platform.

A plugin joins the platform by POSTing its manifest to `POST /plugins` (the
platform registry, architecture §2). Memory declares: its name (`memory`), its
`base_url`, its `mcp_path` (`/mcp`), its `health_path` (`/health`), and its
SURFACE MAPPING — `{"/mcp": "main"}` (one surface, composed onto the platform's
`main` surface alongside governance's). Memory has no isolated surface.

Registration is BEST-EFFORT and RETRYABLE (architecture §3: hot-pluggable, no
platform restart). Mirrors governance's `registration.py`:
`register_with_platform` swallows transport errors and returns False (logging) so
a briefly-down platform can't crash the plugin. The caller IS a loop:
`registration_heartbeat` re-POSTs the manifest every beat for the app's whole
lifespan (issue #39) — the platform's registry is in-memory, so a platform
restart empties it and only a re-assert from this side heals the composed
surfaces. The platform's `POST /plugins` is an idempotent upsert (200 on
re-register); a 409 from an older platform is also treated as success.
"""

from __future__ import annotations

import logging
from functools import partial

import anyio
import httpx

from snowline_memory import config

log = logging.getLogger("snowline_memory.registration")

PLUGIN_NAME = "memory"


def build_manifest(base_url: str | None = None) -> dict:
    """The manifest memory hands the platform. `base_url` defaults to
    `config.base_url()` (where this plugin advertises itself)."""
    return {
        "name": PLUGIN_NAME,
        "base_url": (base_url or config.base_url()),
        "mcp_path": "/mcp",
        "health_path": "/health",
        # Plugin-path -> platform named-surface (gateway.md §2): memory's one
        # surface composes onto `main`. No isolated surface.
        "surfaces": {"/mcp": "main"},
    }


def register_with_platform(
    platform_url: str | None = None,
    base_url: str | None = None,
    *,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> bool:
    """POST the manifest to the platform's `POST /plugins`. Best-effort: returns
    True on a successful register (201) or an idempotent 409 (already registered),
    False on any transport error or non-2xx/409 status — NEVER raises, so a
    briefly-down platform can't crash the plugin (the caller — the registration
    heartbeat — just beats again next interval).
    """
    platform = (platform_url or config.platform_url()).rstrip("/")
    url = f"{platform}/plugins"
    manifest = build_manifest(base_url)
    try:
        if client is not None:
            resp = client.post(url, json=manifest, timeout=timeout)
        else:
            resp = httpx.post(url, json=manifest, timeout=timeout)
    except httpx.HTTPError as exc:
        log.warning("plugin registration to %s failed (will retry): %s", url, exc)
        return False
    if resp.status_code == httpx.codes.CONFLICT:
        log.info("plugin %r already registered with the platform", PLUGIN_NAME)
        return True
    if resp.status_code == httpx.codes.CREATED:
        log.info("registered plugin %r with the platform at %s", PLUGIN_NAME, platform)
        return True
    if resp.is_success:
        # The heartbeat's steady state — a 200 re-assert (upsert unchanged/
        # updated) every beat. DEBUG, or the log fills with a line per interval.
        log.debug(
            "re-asserted plugin %r with the platform at %s", PLUGIN_NAME, platform
        )
        return True
    log.warning(
        "plugin registration to %s returned %s (will retry)", url, resp.status_code
    )
    return False


async def registration_heartbeat(
    platform_url: str | None = None,
    base_url: str | None = None,
    *,
    interval: float | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Re-assert this plugin's registration every `interval` seconds until
    cancelled (issue #39) — the first beat fires immediately, so this loop IS
    the boot registration too. The platform's registry is in-memory: a platform
    restart empties it, and the next beat re-upserts this plugin, so a deploy
    (or 3am crash-restart) heals within one interval instead of requiring this
    plugin to also be kickstarted.

    Each beat is `register_with_platform` (never raises), run off the event loop
    because it's a blocking httpx POST. `interval` defaults to
    `config.registration_heartbeat_seconds()`. A failed beat is already logged
    by `register_with_platform`; the loop just keeps beating. Cancellation
    (lifespan shutdown) unwinds cleanly through the `anyio.sleep`."""
    beat_every = (
        interval if interval is not None else config.registration_heartbeat_seconds()
    )
    beat = partial(register_with_platform, platform_url, base_url, client=client)
    while True:
        try:
            await anyio.to_thread.run_sync(beat)
        except Exception:  # backstop — one bad beat must not kill the heartbeat
            log.exception("registration heartbeat beat failed; continuing")
        await anyio.sleep(beat_every)
