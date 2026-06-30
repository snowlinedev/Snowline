"""Platform configuration — env-driven, with no hardcoded trust.

`SNOWLINE_TRUSTED_CIDRS` is a comma-separated list of CIDRs the platform trusts
as its network gate. It defaults to Tailscale's tailnet range so the tailnet is
trusted out of the box; override it to narrow trust (e.g. a single tailnet IP)
or to add ranges. Trust is configuration, never hardcoded into the request path.
"""

import os

# Tailscale's tailnet (CGNAT) range — the default trusted network.
DEFAULT_TRUSTED_CIDRS = "100.64.0.0/10"

# Local libpq defaults: unix socket, current OS user, no password — mirrors the
# monolith's substrate config. Scopes are the platform's first persisted data.
DEFAULT_DATABASE_URL = "postgresql+psycopg:///snowline_platform"

# Health poller cadence (health.md): poll every plugin every INTERVAL seconds,
# each check bounded by TIMEOUT so one slow plugin can't stall the round.
DEFAULT_HEALTH_POLL_INTERVAL = 15.0
DEFAULT_HEALTH_POLL_TIMEOUT = 5.0

# The named MCP surfaces the gateway mounts at startup. `main` is the composed
# daily-driver surface; `shadow` is the isolated speculation surface (decision
# 8a7f0a11). Surfaces are mounted at create_app time (before any plugin has
# registered), so the set is configuration, not derived from live manifests —
# adding a surface is `SNOWLINE_SURFACES=...` + a restart, not a code edit. See
# the `gateway_app` module docstring for why startup-mount + run()-once preclude
# pure manifest-driven derivation.
DEFAULT_SURFACES = "main,shadow"


def trusted_cidrs() -> list[str]:
    raw = os.environ.get("SNOWLINE_TRUSTED_CIDRS", DEFAULT_TRUSTED_CIDRS)
    return [c.strip() for c in raw.split(",") if c.strip()]


def database_url() -> str:
    return os.environ.get("SNOWLINE_PLATFORM_DATABASE_URL", DEFAULT_DATABASE_URL)


def health_poll_interval() -> float:
    return float(
        os.environ.get(
            "SNOWLINE_HEALTH_POLL_INTERVAL", DEFAULT_HEALTH_POLL_INTERVAL
        )
    )


def health_poll_timeout() -> float:
    return float(
        os.environ.get(
            "SNOWLINE_HEALTH_POLL_TIMEOUT", DEFAULT_HEALTH_POLL_TIMEOUT
        )
    )


def surfaces() -> tuple[str, ...]:
    """The named surfaces the gateway mounts, from `SNOWLINE_SURFACES`
    (comma-separated, default `"main,shadow"`). Order-preserving + deduped;
    `gateway_app.ROOT_SURFACE` is always included (it's the daily-driver root
    at `/mcp`) even when the env omits it. Mounting (not derivation) is forced by
    the startup boot order — see the `gateway_app` module docstring."""
    from snowline_platform.gateway_app import ROOT_SURFACE

    raw = os.environ.get("SNOWLINE_SURFACES", DEFAULT_SURFACES)
    names = [s.strip() for s in raw.split(",") if s.strip()]
    if ROOT_SURFACE not in names:
        names.insert(0, ROOT_SURFACE)
    # Dedupe, preserving first-seen order.
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return tuple(ordered)
