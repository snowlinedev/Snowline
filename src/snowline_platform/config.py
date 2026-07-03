"""Platform configuration — env-driven, with no hardcoded trust.

`SNOWLINE_TRUSTED_CIDRS` is a comma-separated list of CIDRs the platform trusts
as its network gate. It defaults to Tailscale's tailnet range so the tailnet is
trusted out of the box; override it to narrow trust (e.g. a single tailnet IP)
or to add ranges. Trust is configuration, never hardcoded into the request path.
"""

import os
import re

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


# Surface names ride in routes (`/<surface>/mcp`, gateway_app.surface_route), so
# they carry the SAME url-safe-slug shape plugin names do (manifest.py
# PLUGIN_NAME_RE): lowercase alphanumerics + hyphens, starting alphanumeric. In
# particular no `/`, no whitespace, and no `*` — on the LEFT side of a
# SNOWLINE_SURFACE_PLUGINS entry `*` is a config error, it is only legal as the
# RIGHT-hand allow-all sentinel.
_SURFACE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def dashboard_dist() -> str | None:
    """Where the dashboard's built static bundle lives (ui-shell.md §6).

    `SNOWLINE_DASHBOARD_DIST` overrides; the default is the repo checkout's
    `dashboard/dist` (the services run from the checkout via editable
    installs). Returns None when the directory doesn't exist — the app then
    simply doesn't serve `/ui` (dev environments and unit tests don't build
    the frontend)."""
    import pathlib

    raw = os.environ.get("SNOWLINE_DASHBOARD_DIST")
    path = (
        pathlib.Path(raw)
        if raw
        else pathlib.Path(__file__).resolve().parents[2] / "dashboard" / "dist"
    )
    return str(path) if path.is_dir() else None


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

    Interplay with `SNOWLINE_SURFACE_PLUGINS` (issue #36): THIS env alone decides
    the mounted set — a surface named in an allowlist is NOT auto-included. An
    operator composing a constrained surface lists it in BOTH envs; naming an
    unmounted surface in the allowlist is rejected at boot
    (`validate_surface_plugins`, called from `build_surface_mounts`) so a typo'd
    left-hand name fails loud instead of mounting a dead surface while the real
    one stays allow-all. ROOT_SURFACE stays the one magic name — always present
    regardless of the env."""
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
    rationale). Malformed means: an entry without exactly one ``=``; a surface
    name that is empty or not a url-safe slug (``*`` is only legal on the RIGHT
    side); a surface listed twice; an empty allowlist or a stray comma (an empty
    plugin name); a plugin token that violates the manifest name rule
    (manifest.py `PLUGIN_NAME_RE` — a token like ``Governance`` could never name
    a registered plugin, so it would silently empty the surface); or ``*`` mixed
    with explicit names.

    This validates the env's SHAPE only. The cross-check that every named
    surface is actually in the mounted set is `validate_surface_plugins`, run
    once at mount time (`gateway_app.build_surface_mounts`) where the surface
    set is known."""
    # The plugin tokens must be able to name a registered plugin, so they are
    # held to the manifest's own name rule (a deliberate import, not a copy —
    # if the manifest rule evolves, the allowlist rule follows).
    from snowline_platform.manifest import PLUGIN_NAME_RE
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
        if not _SURFACE_NAME_RE.match(name):
            raise ConfigError(
                f"SNOWLINE_SURFACE_PLUGINS entry {entry!r} has an invalid surface "
                f"name {name!r} — surface names must be lowercase url-safe slugs "
                f"([a-z0-9][a-z0-9-]*); '*' is only legal on the right-hand side "
                f"(as in 'main=*')"
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
        for token in tokens:
            if not PLUGIN_NAME_RE.match(token):
                raise ConfigError(
                    f"SNOWLINE_SURFACE_PLUGINS allowlist for surface {name!r} has "
                    f"an invalid plugin name {token!r} — plugin names are "
                    f"lowercase url-safe slugs ([a-z0-9][a-z0-9-]*, the manifest "
                    f"name rule), so this token could never match a registered "
                    f"plugin and would silently leave the surface empty"
                )
        mapping[name] = frozenset(tokens)
    return mapping


def validate_surface_plugins(
    surface_plugins_map: dict[str, frozenset[str] | None],
    mounted_surfaces: tuple[str, ...],
) -> None:
    """Cross-check the parsed allowlists against the MOUNTED surface set — every
    surface named in `SNOWLINE_SURFACE_PLUGINS` must be in `SNOWLINE_SURFACES`
    (operators list a constrained surface in BOTH envs; issue #36 review).

    Raises `ConfigError` for an allowlist naming an unmounted surface. This is
    the typo guard on the LEFT-hand side: with auto-include, ``coer=governance``
    (misspelled ``core``) would mount a dead `/coer/mcp` while the real `core`
    surface stayed ALLOW-ALL — the silent-widening failure mode this feature
    exists to prevent. Called once at mount time (`build_surface_mounts`), where
    the surface set is known, so a bad config kills boot."""
    unmounted = [s for s in surface_plugins_map if s not in mounted_surfaces]
    if unmounted:
        raise ConfigError(
            f"SNOWLINE_SURFACE_PLUGINS names surface(s) "
            f"{', '.join(repr(s) for s in sorted(unmounted))} not present in the "
            f"mounted surface set {tuple(mounted_surfaces)!r} — an allowlist for "
            f"an unmounted surface constrains nothing (a typo here would leave "
            f"the real surface allow-all). Add the surface to SNOWLINE_SURFACES "
            f"(a constrained surface is listed in BOTH envs) or fix the name."
        )
