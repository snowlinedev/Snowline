"""Platform configuration — env-driven, with no hardcoded trust.

`SNOWLINE_TRUSTED_CIDRS` is a comma-separated list of CIDRs the platform trusts
as its network gate. It defaults to Tailscale's tailnet range so the tailnet is
trusted out of the box; override it to narrow trust (e.g. a single tailnet IP)
or to add ranges. Trust is configuration, never hardcoded into the request path.
"""

import os

# Tailscale's tailnet (CGNAT) range — the default trusted network.
DEFAULT_TRUSTED_CIDRS = "100.64.0.0/10"


def trusted_cidrs() -> list[str]:
    raw = os.environ.get("SNOWLINE_TRUSTED_CIDRS", DEFAULT_TRUSTED_CIDRS)
    return [c.strip() for c in raw.split(",") if c.strip()]
