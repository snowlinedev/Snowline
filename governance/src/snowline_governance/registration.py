"""Registering the governance plugin with the platform.

A plugin joins the platform by POSTing its manifest to `POST /plugins` (the
platform registry, architecture §2). Governance declares: its name (`governance`),
its `base_url`, its `mcp_path` (`/mcp`), its `health_path` (`/health`), and its
SURFACE MAPPING — `/mcp -> main` (the real-write decision + artifact tools + the
reads land on the platform's composed `main` surface) AND `/shadow/mcp -> shadow`
(the speculation write tools + read-real grounding, NO real-write — the isolation
property, decision 8a7f0a11).

Registration is BEST-EFFORT and RETRYABLE (architecture §3: hot-pluggable, no
platform restart). The platform may be briefly down when this plugin boots; that
must NOT crash the plugin. `register_with_platform` swallows transport errors and
returns False (logging), so the app starts regardless. The caller IS a loop:
`registration_heartbeat` re-POSTs the manifest every beat for the app's whole
lifespan (issue #39) — the platform's registry is in-memory, so a platform
restart empties it and only a re-assert from this side heals the composed
surfaces. The platform's `POST /plugins` is an idempotent upsert (200 on
re-register); a 409 from an older platform is also treated as success.
"""

from __future__ import annotations

import contextlib
import logging
from functools import partial

import anyio
import httpx

from snowline_governance import config

log = logging.getLogger("snowline_governance.registration")

PLUGIN_NAME = "governance"


def build_manifest(base_url: str | None = None) -> dict:
    """The manifest governance hands the platform. `base_url` defaults to
    `config.base_url()` (where this plugin advertises itself)."""
    return {
        "name": PLUGIN_NAME,
        "base_url": (base_url or config.base_url()),
        "mcp_path": "/mcp",
        "health_path": "/health",
        # Plugin-path -> platform named-surface (gateway.md §2): the real-write
        # decision + artifact tools on `/mcp` compose onto the platform's `main`
        # surface; the speculation surface on `/shadow/mcp` (shadow writes +
        # read-real grounding, NO real-write) composes onto `shadow` — the
        # isolation mirrors the MCP isolation (decision 8a7f0a11).
        "surfaces": {"/mcp": "main", "/shadow/mcp": "shadow"},
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
        # Only an OLDER (pre-upsert) platform returns 409 — but against one it
        # is the heartbeat's per-beat steady state, so DEBUG like the 200 path.
        log.debug("plugin %r already registered with the platform", PLUGIN_NAME)
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

    Each beat is `register_with_platform` (which never raises), run off the
    event loop because it's a blocking httpx POST; the try/except backstops the
    thread-dispatch machinery around it. `interval` defaults to
    `config.registration_heartbeat_seconds()` (lenient — a malformed shared env
    var must not kill the loop). A failed beat is already logged by
    `register_with_platform`; the loop just keeps beating. Cancellation
    (lifespan shutdown) unwinds cleanly through the `anyio.sleep`."""
    beat_every = (
        interval if interval is not None else config.registration_heartbeat_seconds()
    )
    own_client = client is None
    confirmed = False
    try:
        while True:
            try:
                if own_client and client is None:
                    # One long-lived client for the loop's lifetime — a per-beat
                    # client would re-load the CA bundle and re-handshake TCP
                    # every interval. Constructed INSIDE the backstop: this can
                    # raise (e.g. a broken SSL_CERT_FILE), and the heartbeat
                    # rides the lifespan task group — an escaped exception here
                    # would cancel that task group for the process lifetime
                    # while /health stays green (in governance's case, also
                    # killing the sibling webhook_delivery_loop).
                    client = httpx.Client(timeout=10.0)
                beat = partial(
                    register_with_platform, platform_url, base_url, client=client
                )
                # abandon_on_cancel: shutdown must not wait out a beat blocked
                # on an unreachable platform (up to the POST timeout).
                ok = await anyio.to_thread.run_sync(beat, abandon_on_cancel=True)
                if ok and not confirmed:
                    # One guaranteed INFO confirmation per process boot. The
                    # steady-state 200 re-assert logs at DEBUG, so without this
                    # a restart against an already-up platform (200, not 201)
                    # would log NO registration line at all — a healthy
                    # heartbeat and one that never started would look identical.
                    confirmed = True
                    log.info(
                        "plugin %r registration confirmed; heartbeat every %ss",
                        PLUGIN_NAME,
                        beat_every,
                    )
            except Exception:  # backstop — one bad beat must not kill the loop
                log.exception("registration heartbeat beat failed; continuing")
            await anyio.sleep(beat_every)
    finally:
        if own_client and client is not None:
            # An abandoned in-flight beat may still hold this client when a
            # shutdown cancel lands; closing under it makes that beat fail in
            # the background (a stray shutdown-time WARNING at worst — noise,
            # not data loss). Suppress close-time errors from the race.
            with contextlib.suppress(Exception):
                client.close()
