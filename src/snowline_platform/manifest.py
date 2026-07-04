"""A plugin's self-declaration — how it tells the platform where it lives.

A plugin is an out-of-process module addressed by URL (local or cross-tailnet),
so the manifest is just the coordinates the platform needs to compose and
health-check it. The platform never imports plugin code; it routes to `base_url`.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Plugin names are used in gateway routes (/<name>/mcp/...), so keep them a
# url-safe slug: lowercase alphanumerics and hyphens, starting alphanumeric.
# PUBLIC: `config.surface_plugins()` validates the plugin tokens of
# `SNOWLINE_SURFACE_PLUGINS` against THIS rule, so a token that could never
# name a registered plugin fails at boot instead of silently matching nothing.
PLUGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# --- UI block (ui-shell.md §3/§4) ------------------------------------------
#
# These constants are the PLATFORM's source of truth for the UI contract's
# version + kind-name vocabulary. They are NOT used to validate a manifest's
# `ui` block (see `UIWidget`/`UIPage`/`UIBlock` below — `kind` stays a free
# string and an unknown `kind`/`contract_version` registers fine per spec §3
# "fails visible", not at registration). They exist so a drift-guard test
# (tests/test_ui_contract_drift.py, mirroring
# governance/tests/test_contract_drift.py) can pin the published SDK's copy
# (`snowline_plugin_sdk.ui`) equal to this one — the two must never silently
# fork, the same discipline as the governance event contract.
UI_CONTRACT_VERSION: int = 1

UI_WIDGET_KINDS: frozenset[str] = frozenset({"stat", "list"})
UI_PAGE_KINDS: frozenset[str] = frozenset({"table", "thread", "document"})
UI_KINDS: frozenset[str] = UI_WIDGET_KINDS | UI_PAGE_KINDS

# `thread` pages' optional `composer` block (shadow-conversations.md §4) — the
# first activation of the write seam ui-shell.md §4.3/§5 reserved. Same
# drift-guard treatment as UI_KINDS above: the SDK ships an identical
# COMPOSER_FIELDS constant, pinned equal by test_ui_contract_drift.py, so a
# plugin author has one documented field vocabulary and the two copies can't
# silently fork.
COMPOSER_FIELDS: frozenset[str] = frozenset({"endpoint", "placeholder", "disabled_when"})

# Route path-param segments template verbatim into `data` (ui-shell.md §3):
# `{name}` where `name` is a simple identifier. A literal segment is a
# generic url-safe token (letters/digits/`_`/`-`/`.`) — permissive on purpose,
# since routes are plugin-chosen slugs, not a fixed vocabulary.
_ROUTE_PARAM_RE = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\}$")
_ROUTE_LITERAL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _valid_path_segments(path: str, label: str) -> None:
    """The shared per-segment shape rule for `route` and write `endpoint`
    paths: each segment is a literal token or a whole '{name}' param. `label`
    names the field in errors ('route', 'composer endpoint', later
    'action endpoint') so one walk serves every path-shaped field."""
    for segment in path[1:].split("/"):
        if not segment:
            raise ValueError(
                f"{label} {path!r} has an empty path segment (a stray '//')"
            )
        if "{" in segment or "}" in segment:
            if not _ROUTE_PARAM_RE.match(segment):
                raise ValueError(
                    f"{label} {path!r} has a malformed path param {segment!r} — "
                    "expected a whole '{name}' segment with a valid identifier"
                )
        elif not _ROUTE_LITERAL_RE.match(segment):
            raise ValueError(
                f"{label} {path!r} has an invalid path segment {segment!r}"
            )


def _valid_ui_route(route: str) -> str:
    if not route.startswith("/"):
        raise ValueError(f"route {route!r} must start with '/'")
    if route == "/":
        return route
    if route.endswith("/"):
        raise ValueError(f"route {route!r} must not end with a trailing '/'")
    _valid_path_segments(route, "route")
    return route


def _valid_ui_data(data: str) -> str:
    # data paths are plugin-relative and proxied through /ui-api (§5); the
    # proxy's path allowlist depends on every manifest-declared data/endpoint
    # path already living under /ui-api/, so that's enforced here at
    # registration too (belt + suspenders with the proxy's own allowlist).
    if not data.startswith("/ui-api/"):
        raise ValueError(f"data path {data!r} must start with '/ui-api/'")
    return data


def _path_param_names(path: str) -> set[str]:
    """The `{name}` template segment names in a route/endpoint path (both use
    the same '{param}' segment shape, ui-shell.md §3)."""
    return {
        segment[1:-1]
        for segment in path.strip("/").split("/")
        if _ROUTE_PARAM_RE.match(segment)
    }


def _valid_ui_endpoint(endpoint: str, label: str = "composer endpoint") -> str:
    # A composer/action endpoint is a POST write target proxied through
    # /ui-api (shadow-conversations.md §3) — same '/ui-api/' confinement rule
    # as `data`, plus the shared per-segment shape rule, since the proxy's
    # write-path matcher (ui_api.py) walks it segment by segment the same way.
    # `label` keeps the 422s honest when actions[].endpoint reuses this.
    if not endpoint.startswith("/ui-api/"):
        raise ValueError(f"{label} {endpoint!r} must start with '/ui-api/'")
    if endpoint.endswith("/"):
        raise ValueError(f"{label} {endpoint!r} must not end with a trailing '/'")
    _valid_path_segments(endpoint, label)
    # '.'/'..' pass the literal-token regex (routes may use dots in slugs) but
    # the proxy dot-collapses every request path BEFORE matching, so a literal
    # dot-segment in a declared endpoint could never match anything — a
    # registered-but-dead write seam. Fail loud here instead.
    segments = set(endpoint[1:].split("/"))
    if segments & {".", ".."}:
        raise ValueError(
            f"{label} {endpoint!r} contains a '.'/'..' segment — the proxy "
            "normalizes dot-segments away before matching, so this endpoint "
            "could never be reached"
        )
    return endpoint


def _no_duplicate_ids(items: list, what: str) -> None:
    ids = [item.id for item in items]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate {what} id(s) within the plugin: {dupes!r}")


class UIWidget(BaseModel):
    """One home-grid widget contribution (ui-shell.md §3/§4.1).

    `extra="forbid"` — unknown FIELDS fail loud at registration (same posture
    as `UIBlock`); only unknown `kind` STRINGS fail visible at render (§4.4).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="unique within the plugin's widgets")
    slot: Literal["home"] = Field(
        description="placement slot; v1 has exactly one: the home dashboard grid"
    )
    kind: str = Field(
        description="rendering kind (§4.1) — NOT validated against a known list: "
        "kinds are shell-version-dependent and an unknown one fails visible at "
        "render (§4.4), not at registration"
    )
    title: str | None = None
    data: str = Field(description="plugin-relative path, proxied via /ui-api (§5)")
    refresh_seconds: int | None = Field(
        default=None, description="shell polling hint; the shell may clamp it"
    )

    _valid_data = field_validator("data")(_valid_ui_data)


class UIComposer(BaseModel):
    """A `thread` page's optional write seam (shadow-conversations.md §4): an
    input-shaped POST target rendered as a markdown textarea + send button at
    the thread foot. NOT an §4.3 `action` (those are button-shaped with
    confirm semantics) — but both share the same proxy-POST enablement and
    endpoint-allowlist posture (ui-shell.md §5).

    `extra="forbid"` — an unknown field (typo, or a future field an older
    platform doesn't know) rejects the whole manifest (422), same fail-loud
    posture as `UIBlock`.
    """

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(
        description="plugin-relative POST target, proxied via /ui-api (§5); "
        "may contain '{param}' segments matching the page's route params"
    )
    placeholder: str | None = Field(
        default=None, description="composer textarea placeholder text"
    )
    disabled_when: str | None = Field(
        default=None,
        description="a flag name the shell looks for in the thread "
        "response's top-level `flags` list to grey out the composer "
        "(e.g. 'archived') — the plugin owns the semantics",
    )

    _valid_endpoint = field_validator("endpoint")(_valid_ui_endpoint)


class UIPage(BaseModel):
    """One page contribution (ui-shell.md §3/§4.2).

    `extra="forbid"` — now that pages carry LOAD-BEARING optional fields, a
    typo'd `composer` key must 422 at registration, not silently drop and
    leave the write seam dead (every POST 403ing with nothing to explain why).
    Unknown `kind` STRINGS still fail visible at render (§4.4).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="unique within the plugin's pages")
    route: str = Field(
        description="shell route, namespaced by the shell to /<plugin>/<route>; "
        "path params are '{name}' segments that template verbatim into `data`"
    )
    title: str | None = None
    nav: bool = Field(
        default=False, description="appears in the shell nav under the plugin"
    )
    kind: str = Field(
        description="rendering kind (§4.2) — NOT validated, same fail-visible "
        "posture as UIWidget.kind"
    )
    data: str = Field(description="plugin-relative path, proxied via /ui-api (§5)")
    composer: UIComposer | None = Field(
        default=None,
        description="optional write seam, valid only on 'thread' pages "
        "(shadow-conversations.md §4)",
    )

    _valid_data = field_validator("data")(_valid_ui_data)
    _valid_route = field_validator("route")(_valid_ui_route)

    @model_validator(mode="after")
    def _valid_composer_for_kind(self) -> "UIPage":
        if self.composer is None:
            return self
        # composer is input-shaped and only makes sense on the thread view
        # (§4.3); a composer on any other kind is a manifest authoring error,
        # not a shell-version concern, so it fails loud at registration —
        # unlike an unknown `kind` string itself, which fails visible (§4.4).
        if self.kind != "thread":
            raise ValueError(
                f"composer is only valid on 'thread' pages (page {self.id!r} "
                f"has kind {self.kind!r})"
            )
        route_params = _path_param_names(self.route)
        endpoint_params = _path_param_names(self.composer.endpoint)
        unknown = endpoint_params - route_params
        if unknown:
            raise ValueError(
                f"composer endpoint {self.composer.endpoint!r} references "
                f"param(s) {sorted(unknown)!r} not present in route "
                f"{self.route!r}"
            )
        return self


class UIBlock(BaseModel):
    """The manifest's optional `ui` object (ui-shell.md §3).

    `extra="forbid"` on THIS model only — an unknown top-level field (a typo'd
    key, or a future field an older platform doesn't know) rejects the whole
    manifest (422), the same fail-loud posture as `SNOWLINE_SURFACE_PLUGINS`.
    `contract_version` guards this BLOCK's shape, not the kind vocabulary: a
    newer/older value than this platform's `UI_CONTRACT_VERSION` still
    registers fine — the shell degrades to the §4.4 placeholder for versions
    it doesn't render, rather than bricking registration.
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: int = UI_CONTRACT_VERSION
    widgets: list[UIWidget] = Field(default_factory=list)
    pages: list[UIPage] = Field(default_factory=list)

    @field_validator("widgets")
    @classmethod
    def _unique_widget_ids(cls, v: list[UIWidget]) -> list[UIWidget]:
        _no_duplicate_ids(v, "widget")
        return v

    @field_validator("pages")
    @classmethod
    def _unique_page_ids(cls, v: list[UIPage]) -> list[UIPage]:
        _no_duplicate_ids(v, "page")
        return v


# --- Replication block (replication-continuity.md §4/§9 item 2) -----------
#
# The registry stores this block; nothing else reads it. Gateway and health
# only ever read `name`/`base_url`/`health_path`/`surfaces`/`mcp_path` (see
# gateway.py / health.py), so a `replication` block on a manifest is inert to
# both by construction. It is advisory metadata the future pairing step (§5)
# consumes; the platform never routes events itself.


class ReplicationBlock(BaseModel):
    """The manifest's optional `replication` object (replication-continuity.md
    §4): a plugin's self-declaration that it participates in hub-and-spoke
    replication.

    `extra="forbid"` — same fail-loud posture as `UIBlock`: an unknown
    top-level field (a typo, or a future field an older platform doesn't
    know) rejects the whole manifest at registration.

    Absent block = plugin does not replicate; it degrades alone per §4. A
    present block is stored as-is by the registry — advisory metadata read
    only by the pairing step (§5), never by the gateway or health checker.
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: int = Field(
        description="the replication envelope contract version (§3.2) this "
        "plugin's SDK copy speaks; the platform does not validate it against "
        "its own constant — pairing (§5) is what warns on a version mismatch "
        "between two instances' copies of a plugin"
    )
    ingest_path: str = Field(
        description="where the plugin receives peers' signed events, "
        "relative to base_url (SDK-provided handler, §4)"
    )
    events: list[str] = Field(
        default_factory=list,
        description="the event-type vocabulary this plugin emits, declared "
        "so pairing (§5) can warn on vocabulary skew between instances",
    )


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
    ui: UIBlock | None = Field(
        default=None,
        description="optional declarative widget/page contributions (ui-shell.md "
        "§3); None for a headless plugin with no shell contributions",
    )
    replication: ReplicationBlock | None = Field(
        default=None,
        description="optional replication contract declaration "
        "(replication-continuity.md §4); advisory metadata only — the "
        "gateway and health checker never read it. None if the plugin does "
        "not participate in replication",
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
        if not PLUGIN_NAME_RE.match(v):
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
