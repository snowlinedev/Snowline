"""Memory plugin configuration — env-driven.

Memory has its OWN database, separate from the platform's and from governance's
(it owns the working-memory store; the platform owns scopes). It references scopes
by slug as a SOFT, optional reference — it never resolves a slug against the
platform, so it does not need a scope read client, only the platform's
registration endpoint.

Env vars:
  SNOWLINE_MEMORY_DATABASE_URL — the memory store (its own Postgres DB).
  SNOWLINE_PLATFORM_URL        — where the platform runs (the plugin
                                 registration endpoint).
  SNOWLINE_MEMORY_BASE_URL     — where THIS plugin runs, the `base_url` it hands
                                 the platform at registration so the gateway can
                                 proxy to it.
  SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS — how often the registration heartbeat
                                 re-asserts this plugin with the platform (issue
                                 #39). Shared (unprefixed) across plugins, like
                                 SNOWLINE_PLATFORM_URL — one deploy knob tunes
                                 every plugin's cadence.
"""

import os

from snowline_plugin_sdk import registration as sdk_registration

# Local libpq defaults (unix socket, current OS user, no password) — mirrors the
# platform/governance DB config. A SEPARATE database: memory owns the working
# memory store, the platform owns scopes, governance owns the decision graph.
DEFAULT_DATABASE_URL = "postgresql+psycopg:///snowline_memory"

# Where the platform lives — the POST /plugins registration endpoint. Defaults to
# a local platform on its conventional dev port.
DEFAULT_PLATFORM_URL = "http://127.0.0.1:8848"

# Where this plugin advertises itself to the platform (the manifest `base_url`).
# A distinct port from governance's (8801) so both can run on one host.
DEFAULT_BASE_URL = "http://127.0.0.1:8802"


def database_url() -> str:
    return os.environ.get("SNOWLINE_MEMORY_DATABASE_URL", DEFAULT_DATABASE_URL)


def platform_url() -> str:
    return os.environ.get("SNOWLINE_PLATFORM_URL", DEFAULT_PLATFORM_URL).rstrip("/")


def base_url() -> str:
    return os.environ.get("SNOWLINE_MEMORY_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def registration_heartbeat_seconds() -> float:
    """The heartbeat cadence — the one shared deploy knob
    (`SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS`, unprefixed, issue #39). Delegates
    to the SDK's shared lenient+finite parser (issue #50), so the "one deploy
    knob tunes every plugin" promise is one parser, not per-plugin copies: a
    malformed/non-finite/sub-1s value warns and falls back rather than killing
    the self-healing loop."""
    return sdk_registration.heartbeat_seconds_from_env()
