"""A plugin's self-declaration — how it tells the platform where it lives.

A plugin is an out-of-process module addressed by URL (local or cross-tailnet),
so the manifest is just the coordinates the platform needs to compose and
health-check it. The platform never imports plugin code; it routes to `base_url`.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Plugin names are used in gateway routes (/<name>/mcp/...), so keep them a
# url-safe slug: lowercase alphanumerics and hyphens, starting alphanumeric.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class PluginManifest(BaseModel):
    name: str = Field(description="unique plugin id / slug, e.g. 'governance'")
    base_url: str = Field(
        description="where the plugin runs, e.g. http://127.0.0.1:8801 or "
        "http://<tailnet-host>:8801 (local OR cross-tailnet)"
    )
    mcp_path: str = Field(
        default="/mcp",
        description="the plugin's MCP surface, relative to base_url",
    )
    ui_path: str | None = Field(
        default=None,
        description="the plugin's UI, relative to base_url; None if headless",
    )
    health_path: str = Field(
        default="/health",
        description="the plugin's health endpoint, relative to base_url",
    )
    surfaces: dict[str, str] = Field(
        default_factory=dict,
        description="map of the plugin's own MCP path -> platform named-surface "
        "(gateway.md §2), e.g. {'/mcp': 'main', '/shadow/mcp': 'shadow'}. The "
        "gateway aggregates every plugin-path mapped to a named surface into the "
        "single surface a client sees; a tool appears on a surface only because a "
        "plugin mapped it there. Empty defaults to {mcp_path: 'main'} (most "
        "plugins map their one surface onto 'main').",
    )
    scopes: list[str] = Field(
        default_factory=list,
        description="declared scope-namespace dependencies (advisory until the "
        "platform's scope service exists)",
    )

    @field_validator("surfaces")
    @classmethod
    def _valid_surfaces(cls, v: dict[str, str]) -> dict[str, str]:
        for plugin_path, named in v.items():
            if not plugin_path.startswith("/"):
                raise ValueError(
                    f"surface key {plugin_path!r} must be a path starting with '/'"
                )
            if not named:
                raise ValueError(
                    f"surface {plugin_path!r} maps to an empty platform surface name"
                )
        return v

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"plugin name {v!r} must be a lowercase url-safe slug "
                "([a-z0-9][a-z0-9-]*)"
            )
        return v

    @field_validator("base_url")
    @classmethod
    def _valid_base_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"base_url {v!r} must start with http:// or https://")
        return v.rstrip("/")
