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
"""

import logging
import math
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

# Registration heartbeat cadence (issue #39) — matches the platform's health-poll
# default, so a platform restart heals in roughly one health round.
DEFAULT_REGISTRATION_HEARTBEAT_SECONDS = 15.0


def database_url() -> str:
    return os.environ.get("SNOWLINE_GOVERNANCE_DATABASE_URL", DEFAULT_DATABASE_URL)


def platform_url() -> str:
    return os.environ.get("SNOWLINE_PLATFORM_URL", DEFAULT_PLATFORM_URL).rstrip("/")


def base_url() -> str:
    return os.environ.get("SNOWLINE_GOVERNANCE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def registration_heartbeat_seconds() -> float:
    """The heartbeat cadence. LENIENT on a malformed/absurd value (warn + fall
    back), unlike the platform's fail-loud config rule: the heartbeat is the
    self-healing mechanism issue #39 exists for, so a typo in this shared env
    var must not kill the loop (a dead heartbeat = a hollow gateway after the
    next platform restart) — and a zero/negative value must not hot-loop POSTs."""
    raw = os.environ.get("SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS")
    if raw is None:
        return DEFAULT_REGISTRATION_HEARTBEAT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "malformed SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS=%r — using the "
            "default %ss",
            raw,
            DEFAULT_REGISTRATION_HEARTBEAT_SECONDS,
        )
        return DEFAULT_REGISTRATION_HEARTBEAT_SECONDS
    if not math.isfinite(value):
        # "inf"/"nan" parse as floats and slip past the < 1.0 floor, but
        # anyio.sleep(inf/nan) never returns — a silent dead heartbeat, the
        # exact failure this lenient parse exists to prevent. Treat like
        # malformed input.
        logging.getLogger(__name__).warning(
            "non-finite SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS=%r — using the "
            "default %ss",
            raw,
            DEFAULT_REGISTRATION_HEARTBEAT_SECONDS,
        )
        return DEFAULT_REGISTRATION_HEARTBEAT_SECONDS
    if value < 1.0:
        logging.getLogger(__name__).warning(
            "SNOWLINE_REGISTRATION_HEARTBEAT_SECONDS=%r is below the 1s floor "
            "— clamping (the heartbeat cannot be disabled by env; stop the "
            "plugin instead)",
            raw,
        )
        return 1.0
    return value
