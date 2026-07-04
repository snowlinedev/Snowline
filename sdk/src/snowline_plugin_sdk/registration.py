"""Shared plugin↔platform registration heartbeat (issue #50).

Every Snowline plugin joins the platform the same way: it POSTs its manifest to
the platform registry (`POST /plugins`) and RE-ASSERTS it on a heartbeat for the
process lifetime (issue #39). The platform registry is in-memory, so a platform
restart empties it and only a re-assert from the plugin side heals the composed
surfaces. This module is that shared mechanism, extracted from the byte-identical
hand-mirrors that had accreted in `governance/` and `memory/` (and a third in
the owner's private plugin repo, a fourth in TypeScript in `walkthrough-mcp`).

It lives in the SDK's `[client]` extra (`snowline-plugin-sdk[client]`, pulling
`httpx` + `anyio`) and is imported EXPLICITLY — the SDK package root stays
import-pure (no httpx), so base installs don't grow deps.

Language-neutral PATTERN CONTRACT (per #50's second comment — a portable spec
plus per-language reference implementations; this is the Python reference,
`walkthrough-mcp/src/registration.ts` the TS one):
  * beat immediately on boot — this loop IS the boot registration;
  * idempotent upsert semantics — 201 (registered) / 200 (re-asserted) / a
    legacy 409 (older pre-upsert platform) are ALL treated as success;
  * lenient + finite interval parse — a malformed/absurd/non-finite value in the
    shared env var must not kill the loop (a dead heartbeat = a hollow gateway
    after the next platform restart);
  * per-beat backstop — one bad beat (transport error, server error, even a
    client-construction failure) never kills the loop;
  * exactly ONE INFO boot-confirmation on the first successful beat, then a
    silent (DEBUG) 200/409 steady state.

The shared-client close race fix — the lazy per-loop `httpx.Client` built inside
the backstop and closed under `contextlib.suppress` — is httpx/Python-SPECIFIC.
Node's per-beat `fetch` (walkthrough-mcp) has no long-lived client to close under
an abandoned beat, so that fix is NOT part of the language-neutral contract.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import re
from collections.abc import Callable
from functools import partial

import anyio
import httpx

# The shared deploy knob (issue #39): unprefixed, so ONE env var tunes every
# plugin's cadence. Default matches the platform's health-poll default, so a
# platform restart heals in roughly one health round.
HEARTBEAT_ENV_VAR = "SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS"
DEFAULT_HEARTBEAT_SECONDS = 15.0

_parser_log = logging.getLogger("snowline_plugin_sdk.registration")


def heartbeat_seconds_from_env(*, log: logging.Logger | None = None) -> float:
    """The heartbeat cadence, read from the shared `SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS`
    env var. LENIENT on a malformed/absurd value (warn + fall back), unlike the
    platform's fail-loud config rule: the heartbeat is the self-healing mechanism
    issue #39 exists for, so a typo in this shared env var must not kill the loop
    (a dead heartbeat = a hollow gateway after the next platform restart) — and a
    zero/negative value must not hot-loop POSTs.

    `log` attributes the fallback warnings to the calling plugin's own logger
    (an operator watching `snowline_<plugin>.*` must see a fat-fingered env var);
    it defaults to this module's logger for direct/anonymous callers.
    """
    log = log or _parser_log
    default = DEFAULT_HEARTBEAT_SECONDS
    raw = os.environ.get(HEARTBEAT_ENV_VAR)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "malformed SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS=%r — using the "
            "default %ss",
            raw,
            default,
        )
        return default
    if not math.isfinite(value):
        # "inf"/"nan" parse as floats and slip past the < 1.0 floor, but
        # anyio.sleep(inf/nan) never returns — a silent dead heartbeat, the
        # exact failure this lenient parse exists to prevent. Treat like
        # malformed input.
        log.warning(
            "non-finite SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS=%r — using the "
            "default %ss",
            raw,
            default,
        )
        return default
    if value < 1.0:
        log.warning(
            "SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS=%r is below the 1s floor "
            "— clamping (the heartbeat cannot be disabled by env; stop the "
            "plugin instead)",
            raw,
        )
        return 1.0
    return value


class HeartbeatHttpxLogFilter(logging.Filter):
    """Drops httpx's per-request INFO line for the registration heartbeat's
    `POST …/plugins` (one line per beat, forever) while letting every OTHER
    httpx request trace through — a plugin also talks httpx for its own reads /
    outbound deliveries, and muting the whole `httpx` logger at WARNING would
    leave live debugging blind."""

    # httpx's line is 'HTTP Request: POST <url> "HTTP/1.1 200 OK"'; anchor on
    # the URL's path ENDING in /plugins so an unrelated outbound POST whose URL
    # merely contains the substring (…/api/plugins/events) still traces through.
    _HEARTBEAT_LINE_RE = re.compile(r'POST \S+/plugins "')

    def filter(self, record: logging.LogRecord) -> bool:
        return not self._HEARTBEAT_LINE_RE.search(record.getMessage())


# One shared instance for the whole process: `addFilter` dedupes by identity,
# so repeated installs (per create_app, uvicorn reload, several plugins
# co-hosted in one test process) attach exactly one filter.
_HEARTBEAT_HTTPX_FILTER = HeartbeatHttpxLogFilter()


def install_heartbeat_httpx_filter() -> None:
    """Attach the heartbeat filter to the `httpx` logger (idempotent). Call once
    from the plugin's app module — without it, httpx logs one INFO line per beat
    forever."""
    logging.getLogger("httpx").addFilter(_HEARTBEAT_HTTPX_FILTER)


def register_with_platform(
    manifest: dict,
    platform_url: str,
    *,
    plugin_name: str,
    log: logging.Logger,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> bool:
    """POST `manifest` to the platform's `POST /plugins`. Best-effort: returns
    True on a successful register (201) or an idempotent 409 (already registered)
    or a 200 re-assert, False on any transport error or other non-2xx status —
    NEVER raises, so a briefly-down platform can't crash the plugin (the caller —
    the registration heartbeat — just beats again next interval).

    `platform_url` is the resolved platform base; `plugin_name`/`log` are the
    caller's identity so the log lines read as the plugin's own.
    """
    platform = platform_url.rstrip("/")
    url = f"{platform}/plugins"
    try:
        if client is not None:
            resp = client.post(url, json=manifest, timeout=timeout)
        else:
            resp = httpx.post(url, json=manifest, timeout=timeout)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # InvalidURL is NOT an HTTPError subclass — without it a fat-fingered
        # platform_url would escape the "never raises" contract.
        log.warning("plugin registration to %s failed (will retry): %s", url, exc)
        return False
    if resp.status_code == httpx.codes.CONFLICT:
        # Only an OLDER (pre-upsert) platform returns 409 — but against one it
        # is the heartbeat's per-beat steady state, so DEBUG like the 200 path.
        log.debug("plugin %r already registered with the platform", plugin_name)
        return True
    if resp.status_code == httpx.codes.CREATED:
        log.info(
            "registered plugin %r with the platform at %s", plugin_name, platform
        )
        return True
    if resp.is_success:
        # The heartbeat's steady state — a 200 re-assert (upsert unchanged/
        # updated) every beat. DEBUG, or the log fills with a line per interval.
        log.debug(
            "re-asserted plugin %r with the platform at %s", plugin_name, platform
        )
        return True
    log.warning(
        "plugin registration to %s returned %s (will retry)", url, resp.status_code
    )
    return False


async def registration_heartbeat(
    manifest_builder: Callable[[], dict],
    platform_url: str,
    *,
    plugin_name: str,
    log: logging.Logger,
    interval: float | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Re-assert this plugin's registration every `interval` seconds until
    cancelled (issue #39) — the first beat fires immediately, so this loop IS
    the boot registration too. The platform's registry is in-memory: a platform
    restart empties it, and the next beat re-upserts this plugin, so a deploy
    (or 3am crash-restart) heals within one interval instead of requiring this
    plugin to also be kickstarted.

    `interval=None` (the production path) resolves through the shared lenient
    env parse, so the default lives HERE, not re-derived in every plugin
    wrapper. A directly-passed interval may be sub-second (tests), but a
    non-finite or negative one is rejected onto the env parse — `sleep(inf)` is
    a silently dead heartbeat and `sleep(<0)` raises out of the loop, the two
    failures the parser's guards exist to prevent.

    `manifest_builder` is called once per beat to produce the manifest to POST
    (kept a callable so it re-reads the plugin's config each beat). Each beat is
    `register_with_platform` (which never raises), run off the event loop because
    it's a blocking httpx POST; the try/except backstops the thread-dispatch
    machinery around it. A failed beat is already logged by
    `register_with_platform`; the loop just keeps beating. Cancellation (lifespan
    shutdown) unwinds cleanly through the `anyio.sleep`."""
    if interval is None:
        interval = heartbeat_seconds_from_env(log=log)
    elif not math.isfinite(interval) or interval < 0:
        log.warning(
            "invalid heartbeat interval %r passed directly — falling back to "
            "the shared env parse",
            interval,
        )
        interval = heartbeat_seconds_from_env(log=log)
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
                    register_with_platform,
                    manifest_builder(),
                    platform_url,
                    plugin_name=plugin_name,
                    log=log,
                    client=client,
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
                        plugin_name,
                        interval,
                    )
            except Exception:  # backstop — one bad beat must not kill the loop
                log.exception("registration heartbeat beat failed; continuing")
            await anyio.sleep(interval)
    finally:
        if own_client and client is not None:
            # An abandoned in-flight beat may still hold this client when a
            # shutdown cancel lands; closing under it makes that beat fail in
            # the background (a stray shutdown-time WARNING at worst — noise,
            # not data loss). Suppress close-time errors from the race.
            with contextlib.suppress(Exception):
                client.close()
