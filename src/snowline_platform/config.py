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


def trusted_cidrs() -> list[str]:
    raw = os.environ.get("SNOWLINE_TRUSTED_CIDRS", DEFAULT_TRUSTED_CIDRS)
    return [c.strip() for c in raw.split(",") if c.strip()]


def database_url() -> str:
    return os.environ.get("SNOWLINE_PLATFORM_DATABASE_URL", DEFAULT_DATABASE_URL)
