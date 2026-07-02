"""`config.surface_plugins()` — the per-surface plugin-allowlist parser (#36).

Covers the default (empty → allow-all), `*`, subsets, whitespace tolerance, the
fail-loud malformed cases (shape, surface-name slugs, plugin-token slugs), and
the mounted-set cross-check (`validate_surface_plugins` — a constrained surface
is listed in BOTH envs, no auto-include). The allowlist is env-only, so each
test drives `SNOWLINE_SURFACE_PLUGINS` via `monkeypatch`."""

from __future__ import annotations

import pytest

from snowline_platform import config
from snowline_platform.config import ConfigError


def test_default_empty_is_allow_all(monkeypatch):
    """Unset (and empty) env → an empty map, i.e. every surface allow-all — the
    backward-compatible default (no surface is constrained)."""
    monkeypatch.delenv("SNOWLINE_SURFACE_PLUGINS", raising=False)
    assert config.surface_plugins() == {}

    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", "   ")
    assert config.surface_plugins() == {}


def test_star_is_allow_all_sentinel(monkeypatch):
    """`surface=*` maps to None — the allow-all sentinel — distinct from an
    (illegal) empty allowlist."""
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", "main=*")
    assert config.surface_plugins() == {"main": None}


def test_subset_and_multiple_surfaces(monkeypatch):
    """The documented example: `main=*` (all) alongside `core=governance` (a
    one-plugin subset); and a multi-plugin subset."""
    monkeypatch.setenv(
        "SNOWLINE_SURFACE_PLUGINS", "main=*;core=governance;audit=governance,other"
    )
    assert config.surface_plugins() == {
        "main": None,
        "core": frozenset({"governance"}),
        "audit": frozenset({"governance", "other"}),
    }


def test_whitespace_tolerant(monkeypatch):
    """Whitespace around surfaces, the '=', plugin names, and the ';'/',' — plus a
    trailing ';' — are all tolerated."""
    monkeypatch.setenv(
        "SNOWLINE_SURFACE_PLUGINS",
        "  main = * ;  core = governance , other ;  ",
    )
    assert config.surface_plugins() == {
        "main": None,
        "core": frozenset({"governance", "other"}),
    }


@pytest.mark.parametrize(
    "raw",
    [
        "main",  # no '='
        "main=governance=extra",  # two '='
        "=governance",  # empty surface name
        "main=*;main=governance",  # duplicate surface
        "core=",  # empty allowlist
        "core=governance,",  # stray trailing comma -> empty token
        "core=,governance",  # stray leading comma -> empty token
        "core=governance,*",  # '*' mixed with names
        # -- surface (LEFT-hand) names must be url-safe slugs ------------------
        "*=governance",  # '*' is only legal on the RIGHT side
        "Core=governance",  # uppercase
        "co er=governance",  # inner whitespace
        "core/mcp=governance",  # '/' (would break the /X/mcp route)
        "-core=governance",  # must start alphanumeric
        # -- plugin (RIGHT-hand) tokens must match the manifest name rule ------
        "core=Governance",  # uppercase — could never name a plugin
        "core=gov ernance",  # inner whitespace
        "core=gov/x",  # '/'
        "core=_governance",  # charset violation (underscore)
    ],
)
def test_malformed_fails_loud(monkeypatch, raw):
    """Every malformed shape raises `ConfigError` — we never silently drop or
    widen a surface (a typo must fail at startup, not quietly expose PM)."""
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", raw)
    with pytest.raises(ConfigError):
        config.surface_plugins()


def test_surfaces_does_not_auto_include_allowlist_named_surface(monkeypatch):
    """`SNOWLINE_SURFACES` ALONE decides the mounted set — a surface named only
    in the allowlist is NOT auto-included (that convenience is gone: it turned
    a left-hand typo into a silently-mounted dead surface while the real one
    stayed allow-all). The mismatch is instead rejected at mount time by
    `validate_surface_plugins`."""
    monkeypatch.setenv("SNOWLINE_SURFACES", "main,shadow")
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", "core=governance")
    assert config.surfaces() == ("main", "shadow")


def test_validate_rejects_allowlist_for_unmounted_surface(monkeypatch):
    """Naming an unmounted surface in the allowlist is a boot-time ConfigError —
    the `coer=governance` typo case: with auto-include it would mount a dead
    /coer/mcp while `core` stayed allow-all. Operators list a constrained
    surface in BOTH envs."""
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", "coer=governance")
    with pytest.raises(ConfigError, match="coer"):
        config.validate_surface_plugins(
            config.surface_plugins(), ("main", "shadow", "core")
        )


def test_validate_accepts_allowlist_named_in_both_envs(monkeypatch):
    """The documented shape — the constrained surface present in BOTH envs —
    validates cleanly (and the empty allowlist trivially so)."""
    monkeypatch.setenv("SNOWLINE_SURFACES", "main,shadow,core")
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", "main=*;core=governance")
    config.validate_surface_plugins(config.surface_plugins(), config.surfaces())

    monkeypatch.delenv("SNOWLINE_SURFACE_PLUGINS")
    config.validate_surface_plugins(config.surface_plugins(), config.surfaces())
