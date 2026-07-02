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

# Per-surface plugin allowlists (`SNOWLINE_SURFACE_PLUGINS`, issue #36): which
# registered plugins a named surface is allowed to aggregate. The empty default
# means every surface aggregates every plugin — today's behavior, fully backward
# compatible. Set e.g. ``"main=*;core=governance"`` to compose a governance-only
# `core` surface (no PM) alongside the full `main` daily driver.
DEFAULT_SURFACE_PLUGINS = ""


class ConfigError(ValueError):
    """A malformed platform config value, raised at startup.

    We FAIL LOUD on a malformed `SNOWLINE_SURFACE_PLUGINS` rather than
    best-effort-parsing because the dangerous failure mode is a typo SILENTLY
    widening a surface: e.g. ``core=goverance`` (misspelled) would leave the
    misspelled name matching no plugin — but a more permissive parser could just
    as easily drop the whole entry and fall back to allow-all, quietly making the
    private PM plugin reachable on a surface meant to be governance-only. A hard
    startup error is strictly safer than a surface that exposes more than
    intended, so the parser validates strictly."""


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
    the startup boot order — see the `gateway_app` module docstring.

    Interplay with `SNOWLINE_SURFACE_PLUGINS` (issue #36): any surface NAMED in
    an allowlist is auto-included in the mounted set (an allowlist for an
    unmounted surface would be dead config). `SNOWLINE_SURFACES` order wins;
    surfaces that appear only in an allowlist are appended after it. ROOT_SURFACE
    stays the one magic name — always present regardless of either env."""
    from snowline_platform.gateway_app import ROOT_SURFACE

    raw = os.environ.get("SNOWLINE_SURFACES", DEFAULT_SURFACES)
    names = [s.strip() for s in raw.split(",") if s.strip()]
    if ROOT_SURFACE not in names:
        names.insert(0, ROOT_SURFACE)
    # Auto-include surfaces referenced only by an allowlist (issue #36
    # nice-to-have): naming a surface in SNOWLINE_SURFACE_PLUGINS pulls it into
    # the mounted set so the allowlist has something to constrain.
    names.extend(surface_plugins().keys())
    # Dedupe, preserving first-seen order.
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return tuple(ordered)


def surface_plugins() -> dict[str, frozenset[str] | None]:
    """Per-surface plugin allowlists from `SNOWLINE_SURFACE_PLUGINS` (issue #36).

    Format: ``"main=*;core=governance,other"`` — ``;``-separated surface entries,
    each ``<surface>=<allowlist>``; the allowlist is ``*`` (every plugin) or a
    ``,``-separated list of plugin names. Whitespace around any token is ignored.

    Returns a map ``surface -> allowlist`` where the value is ``None`` for ``*``
    (allow every plugin) or a `frozenset` of the allowed plugin names. A surface
    NOT present in the returned map defaults to allow-all — so the empty/default
    env is fully backward compatible (every surface aggregates every plugin, the
    behavior before this feature). The gateway applies this at the aggregation
    step (`gateway.discover_upstreams`), filtering by plugin NAME; registration,
    health, and the registry views are untouched.

    Malformed input raises `ConfigError` (see its docstring for the fail-loud
    rationale). Malformed means: an entry without exactly one ``=``; an empty
    surface name; a surface listed twice; an empty allowlist or a stray comma
    (an empty plugin name); or ``*`` mixed with explicit names."""
    raw = os.environ.get("SNOWLINE_SURFACE_PLUGINS", DEFAULT_SURFACE_PLUGINS)
    mapping: dict[str, frozenset[str] | None] = {}
    for segment in raw.split(";"):
        entry = segment.strip()
        if not entry:
            continue  # tolerate a trailing ';' or blank segments
        if entry.count("=") != 1:
            raise ConfigError(
                f"SNOWLINE_SURFACE_PLUGINS entry {entry!r} is malformed — expected "
                f"exactly one '=' as in 'surface=plugin,plugin' or 'surface=*'"
            )
        name_raw, _, allow_raw = entry.partition("=")
        name = name_raw.strip()
        allow = allow_raw.strip()
        if not name:
            raise ConfigError(
                f"SNOWLINE_SURFACE_PLUGINS entry {entry!r} has an empty surface name"
            )
        if name in mapping:
            raise ConfigError(
                f"SNOWLINE_SURFACE_PLUGINS lists surface {name!r} more than once"
            )
        if allow == "*":
            mapping[name] = None
            continue
        tokens = [t.strip() for t in allow.split(",")]
        if any(not t for t in tokens):
            raise ConfigError(
                f"SNOWLINE_SURFACE_PLUGINS allowlist for surface {name!r} has an "
                f"empty plugin name (a stray comma or empty allowlist?) — use '*' "
                f"for all plugins or a comma-separated list of plugin names"
            )
        if "*" in tokens:
            raise ConfigError(
                f"SNOWLINE_SURFACE_PLUGINS allowlist for surface {name!r} mixes '*' "
                f"with explicit plugin names — '*' must stand alone"
            )
        mapping[name] = frozenset(tokens)
    return mapping
