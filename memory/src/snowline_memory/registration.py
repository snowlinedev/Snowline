"""Registering the memory plugin with the platform.

A plugin joins the platform by POSTing its manifest to `POST /plugins` (the
platform registry, architecture §2). Memory declares: its name (`memory`), its
`base_url`, its `mcp_path` (`/mcp`), its `health_path` (`/health`), and its
SURFACE MAPPING — `{"/mcp": "main"}` (one surface, composed onto the platform's
`main` surface alongside governance's). Memory has no isolated surface.

Registration is BEST-EFFORT and RETRYABLE (architecture §3: hot-pluggable, no
platform restart). Mirrors governance's `registration.py`: `register_with_platform`
swallows transport errors and returns False (logging) so a briefly-down platform
can't crash the plugin; a 409 (already registered) is treated as success.
"""

from __future__ import annotations

import logging

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
    briefly-down platform can't crash the plugin (the caller retries).
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
    if resp.is_success:
        log.info("registered plugin %r with the platform at %s", PLUGIN_NAME, platform)
        return True
    log.warning(
        "plugin registration to %s returned %s (will retry)", url, resp.status_code
    )
    return False
