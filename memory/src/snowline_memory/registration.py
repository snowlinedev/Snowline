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

The heartbeat MECHANISM — the POST, the lazy per-loop client, the #51 hardening
(non-finite interval guard, per-beat backstop, one boot-confirmation INFO then
silent steady state, scoped httpx log filter) — lives ONCE in
`snowline_plugin_sdk.registration` (issue #50); this module is just memory's
manifest builder plus thin, plugin-labelled calls into it.
"""

from __future__ import annotations

import logging

from snowline_plugin_sdk import registration as sdk_registration

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
    client=None,
    timeout: float = 10.0,
) -> bool:
    """POST memory's manifest to the platform's `POST /plugins` — a thin,
    plugin-labelled call into the shared SDK client. Best-effort (never raises);
    see `snowline_plugin_sdk.registration.register_with_platform` for the
    idempotent-upsert / 409-as-success / transport-error semantics."""
    return sdk_registration.register_with_platform(
        build_manifest(base_url),
        platform_url or config.platform_url(),
        plugin_name=PLUGIN_NAME,
        log=log,
        client=client,
        timeout=timeout,
    )


async def registration_heartbeat(
    platform_url: str | None = None,
    base_url: str | None = None,
    *,
    interval: float | None = None,
    client=None,
) -> None:
    """Re-assert memory's registration on the shared heartbeat (issue #39) — a
    thin call into `snowline_plugin_sdk.registration.registration_heartbeat`,
    which owns the beat-on-boot / lazy per-loop client / #51 hardening. `interval`
    defaults inside the SDK to the shared lenient env parse
    (`SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS`), with the fallback warnings
    attributed to memory's own logger; the manifest is rebuilt each beat so a
    config change is picked up."""
    await sdk_registration.registration_heartbeat(
        lambda: build_manifest(base_url),
        platform_url or config.platform_url(),
        plugin_name=PLUGIN_NAME,
        log=log,
        interval=interval,
        client=client,
    )
