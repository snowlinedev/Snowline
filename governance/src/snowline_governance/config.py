"""Governance plugin configuration — env-driven.

Governance has its OWN database, separate from the platform's (it owns the
decision graph; the platform owns scopes). It references scopes by slug (a soft
reference) and reads the scope tree from the platform over HTTP, so it also needs
to know where the platform is.

Env vars:
  SNOWLINE_GOVERNANCE_DATABASE_URL — the governance store (its own Postgres DB).
  SNOWLINE_PLATFORM_URL            — where the platform runs (scope reads + the
                                     plugin registration endpoint).
  SNOWLINE_GOVERNANCE_BASE_URL     — where THIS plugin runs, the `base_url` it
                                     hands the platform at registration so the
                                     gateway can proxy to it.
  SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS — how often the registration heartbeat
                                     re-asserts this plugin with the platform
                                     (issue #39). Shared (unprefixed) across
                                     plugins, like SNOWLINE_PLATFORM_URL — one
                                     deploy knob tunes every plugin's cadence.
                                     Parsed (leniently) by
                                     `snowline_plugin_sdk.registration`, not
                                     here — this module stays stdlib-only.
"""

import os

# Local libpq defaults (unix socket, current OS user, no password) — mirrors the
# platform/monolith DB config. A SEPARATE database from the platform's: governance
# owns the decision graph, the platform owns scopes.
DEFAULT_DATABASE_URL = "postgresql+psycopg:///snowline_governance"

# Where the platform lives — the scope read API + the POST /plugins registration
# endpoint. Defaults to a local platform on its conventional dev port.
DEFAULT_PLATFORM_URL = "http://127.0.0.1:8848"

# Where this plugin advertises itself to the platform (the manifest `base_url`).
DEFAULT_BASE_URL = "http://127.0.0.1:8801"


def database_url() -> str:
    return os.environ.get("SNOWLINE_GOVERNANCE_DATABASE_URL", DEFAULT_DATABASE_URL)


def platform_url() -> str:
    return os.environ.get("SNOWLINE_PLATFORM_URL", DEFAULT_PLATFORM_URL).rstrip("/")


def base_url() -> str:
    return os.environ.get("SNOWLINE_GOVERNANCE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
