"""`config.surface_plugins()` — the per-surface plugin-allowlist parser (#36).

Covers the default (empty → allow-all), `*`, subsets, whitespace tolerance, the
fail-loud malformed cases, and the `surfaces()` auto-include interplay. The
allowlist is env-only, so each test drives `SNOWLINE_SURFACE_PLUGINS` via
`monkeypatch`."""

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
    ],
)
def test_malformed_fails_loud(monkeypatch, raw):
    """Every malformed shape raises `ConfigError` — we never silently drop or
    widen a surface (a typo must fail at startup, not quietly expose PM)."""
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", raw)
    with pytest.raises(ConfigError):
        config.surface_plugins()


def test_surfaces_auto_includes_allowlist_named_surface(monkeypatch):
    """A surface named ONLY in the allowlist is auto-included in the mounted set
    (nice-to-have), appended after the `SNOWLINE_SURFACES` order; ROOT_SURFACE
    stays present and first."""
    monkeypatch.setenv("SNOWLINE_SURFACES", "main,shadow")
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", "core=governance")
    assert config.surfaces() == ("main", "shadow", "core")


def test_surfaces_auto_include_deduped(monkeypatch):
    """A surface named in BOTH envs isn't duplicated, and SNOWLINE_SURFACES order
    wins for it."""
    monkeypatch.setenv("SNOWLINE_SURFACES", "main,core,shadow")
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", "core=governance;main=*")
    assert config.surfaces() == ("main", "core", "shadow")
