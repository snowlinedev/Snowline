"""A plugin's self-declaration — how it tells the platform where it lives.

A plugin is an out-of-process module addressed by URL (local or cross-tailnet),
so the manifest is just the coordinates the platform needs to compose and
health-check it. The platform never imports plugin code; it routes to `base_url`.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Plugin names are used in gateway routes (/<name>/mcp/...), so keep them a
# url-safe slug: lowercase alphanumerics and hyphens, starting alphanumeric.
# PUBLIC: `config.surface_plugins()` validates the plugin tokens of
# `SNOWLINE_SURFACE_PLUGINS` against THIS rule, so a token that could never
# name a registered plugin fails at boot instead of silently matching nothing.
PLUGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Plugin names also become top-level shell routes (/<name>/<route>, ui-shell.md
# §3), so the shell's own top-level paths are off limits: the IA sections
# (dashboard-ia.md §3) and the native views System absorbs. Without this, a
# plugin named "review" with a root page out-ranks the platform's /review in
# the router (a trailing-slash pattern beats the static route) and silently
# shadows the section. Fail-loud at registration (422), same posture as the
# name grammar above — this is a registration-time collision, not a
# shell-version-dependent vocabulary, so fail-visible would be wrong here.
RESERVED_PLUGIN_NAMES: frozenset[str] = frozenset(
    {"today", "roadmap", "features", "review", "system", "plugins", "surfaces", "scopes"}
)

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
UI_PAGE_KINDS: frozenset[str] = frozenset({"table", "thread", "document", "board"})
UI_KINDS: frozenset[str] = UI_WIDGET_KINDS | UI_PAGE_KINDS

# `thread` pages' optional `composer` block (shadow-conversations.md §4) — the
# first activation of the write seam ui-shell.md §4.3/§5 reserved. Same
# drift-guard treatment as UI_KINDS above: the SDK ships an identical
# COMPOSER_FIELDS constant, pinned equal by test_ui_contract_drift.py, so a
# plugin author has one documented field vocabulary and the two copies can't
# silently fork.
COMPOSER_FIELDS: frozenset[str] = frozenset({"endpoint", "placeholder", "disabled_when"})

# Page `actions[]` (ui-shell.md §5 — moved from "reserved" to specified, issue
# #123): the button-shaped write seam sibling of `composer`. Same drift-guard
# treatment as COMPOSER_FIELDS — the SDK ships equal copies, pinned by
# test_ui_contract_drift.py — so a plugin author has one documented field
# vocabulary. `ACTION_FIELDS` are the keys of an `actions[]` entry;
# `ACTION_FIELD_FIELDS` are the keys of one declared form field; the shell
# renders a form-field control per `ACTION_FIELD_KINDS` value.
ACTION_FIELDS: frozenset[str] = frozenset({"id", "label", "endpoint", "fields"})
ACTION_FIELD_FIELDS: frozenset[str] = frozenset({"name", "label", "kind", "required"})
ACTION_FIELD_KINDS: frozenset[str] = frozenset({"text", "multiline", "scope"})

# Placement-intent vocabulary (docs/specs/dashboard-ia.md §4.1): a plugin's
# optional declaration of WHERE a page/widget belongs in the platform-owned
# IA. Same drift-guard treatment as UI_KINDS/COMPOSER_FIELDS/ACTION_FIELDS
# above — the SDK ships an identical PLACEMENT_INTENTS constant, pinned equal
# by test_ui_contract_drift.py. `intent` itself is a FREE string on
# UIWidget/UIPage below (fail-visible, same posture as `kind`, §4.2): an
# unrecognized value registers fine and degrades to the plugin-grouped
# fallback group at composition time — this constant documents the
# vocabulary, it does not gate it. NOT the same `intent` as the payload-level
# semantic color hint ('good'|'bad'|'neutral') in stat/list/badge response
# bodies — that one lives in the data plane; this one lives on the manifest.
PLACEMENT_INTENTS: frozenset[str] = frozenset(
    {
        "attention",
        "digest",
        "activity",
        "roadmap",
        "feature-status",
        "review-queue",
        "detail",
        "admin",
    }
)

# Per-intent meaning + placement (dashboard-ia.md §4.1's table), one line
# each: what declaring this intent says about the view, and where the
# platform places it. Same doc-not-schema posture as UI_KIND_SHAPES /
# ACTION_FIELD_SHAPE above — a plain dict for a manifest author to read, not
# validated against anywhere; nothing here enforces PLACEMENT_INTENTS'
# fail-visible posture above. Same drift-guard treatment: the SDK ships an
# identical copy, pinned equal by test_ui_contract_drift.py, and pinned to
# cover PLACEMENT_INTENTS exactly.
PLACEMENT_INTENT_SHAPE: dict[str, str] = {
    "attention": "a needs-you / blocked / overdue surface — Today, first band",
    "digest": "a summary of state (\"since you last looked\") — Today, second "
    "band",
    "activity": "a recent-events feed (shipped, decided, changed) — Today, "
    "third band",
    "roadmap": "planning structure (what's queued, in what order) — Roadmap",
    "feature-status": "cross-scope progress on a named body of work — "
    "Features",
    "review-queue": "items awaiting explicit human judgment — Review",
    "detail": "an entity page reached by links, never browsed to — no nav "
    "entry (subsumes `nav: false`)",
    "admin": "an operational / registry / diagnostic view — System",
}

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


def _valid_ui_action_endpoint(endpoint: str) -> str:
    """A page action's POST endpoint — same `/ui-api/` confinement + per-segment
    shape rule as a composer endpoint (both are proxy-POST write targets, and
    the proxy's write-path matcher walks them identically), just labelled for
    honest 422s."""
    return _valid_ui_endpoint(endpoint, "action endpoint")


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
    intent: str | None = Field(
        default=None,
        description="optional placement intent — see docs/specs/dashboard-ia.md "
        "§4. NOT validated against PLACEMENT_INTENTS: an unrecognized value "
        "fails visible (degrades to the fallback group), same posture as `kind`",
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


class UIActionField(BaseModel):
    """One declared form field of a page `action` (ui-shell.md §5). The shell
    renders one labelled control per field and submits `{name: value}` in the
    action's POST body.

    `extra="forbid"` — an unknown field key (typo, or a future field an older
    platform doesn't know) rejects the whole manifest, same fail-loud posture
    as everything else in the `ui` block. `kind` stays a FREE string (like a
    widget/page `kind`): an unrecognized value fails visible at render (the
    shell falls back to a text control) rather than bricking registration —
    `ACTION_FIELD_KINDS` documents what the shell renders, it does not gate.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="the JSON key the shell submits this field as")
    label: str | None = Field(
        default=None, description="visible field label (defaults to `name`)"
    )
    kind: str = Field(
        default="text",
        description="rendering hint — 'text' (single line), 'multiline' "
        "(textarea), or 'scope' (text input with a typeahead over the "
        "platform's scope slugs); unknown kinds fall back to a text control "
        "at render",
    )
    required: bool = Field(
        default=False, description="the shell blocks submit until this is filled"
    )


class UIAction(BaseModel):
    """One page-level write affordance (ui-shell.md §5 actions[], issue #123):
    a labelled button that opens a minimal form of `fields` and POSTs their
    values through the /ui-api proxy to `endpoint`. The button-shaped sibling of
    the input-shaped `composer` — both ride the same proxy-POST enablement and
    endpoint-allowlist posture (§5); the plugin owns all semantics. On a 2xx the
    shell follows an optional plugin-relative `navigate` href in the response.

    `extra="forbid"` — same fail-loud posture as `UIComposer`: a typo'd or
    unknown key rejects the manifest rather than silently dropping a write seam.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="unique within the page's actions")
    label: str = Field(description="the button text")
    endpoint: str = Field(
        description="plugin-relative POST target, proxied via /ui-api (§5); "
        "may contain '{param}' segments matching the page's route params"
    )
    fields: list[UIActionField] = Field(
        default_factory=list,
        description="the form the shell renders; empty = a bare button that "
        "POSTs an empty body",
    )

    _valid_endpoint = field_validator("endpoint")(_valid_ui_action_endpoint)

    @field_validator("fields")
    @classmethod
    def _unique_field_names(cls, v: list[UIActionField]) -> list[UIActionField]:
        names = [f.name for f in v]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate action field name(s): {dupes!r}")
        return v


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
    actions: list[UIAction] = Field(
        default_factory=list,
        description="optional page-level write affordances (ui-shell.md §5 "
        "actions[]); valid on any page kind, unlike the thread-only composer",
    )
    intent: str | None = Field(
        default=None,
        description="optional placement intent — same contract and fail-visible "
        "posture as UIWidget.intent (docs/specs/dashboard-ia.md §4)",
    )

    _valid_data = field_validator("data")(_valid_ui_data)
    _valid_route = field_validator("route")(_valid_ui_route)

    @field_validator("actions")
    @classmethod
    def _unique_action_ids(cls, v: list[UIAction]) -> list[UIAction]:
        _no_duplicate_ids(v, "action")
        return v

    @model_validator(mode="after")
    def _valid_action_params_for_route(self) -> "UIPage":
        # Every '{param}' an action endpoint templates must exist in the page's
        # route (same rule as the composer below) — an action endpoint keyed on
        # a param the route can't supply is a dead write seam, so it fails loud
        # at registration rather than 403ing forever at request time.
        route_params = _path_param_names(self.route)
        for action in self.actions:
            unknown = _path_param_names(action.endpoint) - route_params
            if unknown:
                raise ValueError(
                    f"action {action.id!r} endpoint {action.endpoint!r} "
                    f"references param(s) {sorted(unknown)!r} not present in "
                    f"route {self.route!r}"
                )
        return self

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



# --- HTTP surfaces (gateway.md §3a) ----------------------------------------
#
# A plugin's optional declaration that it serves a PLAIN-HTTP contract the
# gateway should proxy at the platform root — the vocabulary beyond MCP and
# `ui`. The motivating consumer is musher's work-item provider contract
# (`/provider/work-items…`, served by the pm plugin): with a declared `http`
# surface, `MUSHER_PROVIDER_URL` points at the GATEWAY, not at the plugin's
# own port, so the address survives a plugin redeploy/move and rides the same
# tailnet trust gate as every other platform route.
#
# Unlike `ui`'s `/ui-api/<plugin>/…` namespacing, an http surface's prefix is
# ROOT-LEVEL and public: the path the client sends is the path the plugin
# serves, byte for byte. That makes prefixes a SHARED namespace — hence the
# reserved set below, the within-manifest overlap rules here, and the
# cross-plugin collision refusal in `registry.upsert`.

# The top-level path segments the platform app itself serves — a plugin may
# not claim any of them, or its surface would be a route nobody can reach
# (the catch-all proxy is registered LAST, so a platform route always wins)
# and an operator would be left debugging a silent shadow.
#
# ONE constant, kept honest by a test that walks the BUILT app's routes and
# asserts every top-level first segment appears here
# (tests/test_http_proxy.py::test_reserved_prefixes_cover_every_app_route) —
# so a future platform route can't be added without either reserving it or
# consciously deciding not to.
#
# `shadow` is here because named MCP surfaces mount at `/<surface>/mcp` and
# `config.DEFAULT_SURFACES` is "main,shadow" (the ROOT_SURFACE `main` mounts
# at the bare `/mcp`). A non-default `SNOWLINE_SURFACES` name is NOT
# statically reserved — an operator adding a surface should add it here too;
# routing itself is safe regardless, since surface mounts precede the
# catch-all.
RESERVED_HTTP_PREFIXES: frozenset[str] = frozenset(
    {
        # MCP: the root surface, the default named surface, the platform's own
        # tool app.
        "mcp",
        "shadow",
        "platform",
        # The dashboard shell and its data proxy (ui-shell.md §5–§6).
        "ui",
        "ui-api",
        # Platform JSON routers.
        "plugins",
        "scopes",
        "milestones",
        "surfaces",
        "replication",
        "replication-admin",
        # Bare app routes.
        "health",
        "whoami",
        # FastAPI's own docs routes.
        "docs",
        "redoc",
        "openapi.json",
        # Reserved ahead of use: the event surface the platform would serve
        # under its own name (today replication events live under
        # `/replication/events/…`).
        "events",
    }
)

# The methods the gateway proxy is wired for. A declared method outside this
# set is a manifest error, not a fail-visible degradation: the gateway would
# never forward it, so the plugin's route would be dead on arrival.
HTTP_SURFACE_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE"}
)


def http_prefix_matches(prefix: str, path: str) -> bool:
    """Does `path` fall under `prefix`? Segment-aligned on purpose:
    `/providerx` does NOT match `/provider`, only `/provider` itself and
    anything under `/provider/`."""
    return path == prefix or path.startswith(prefix + "/")


def http_prefixes_collide(a: str, b: str) -> bool:
    """Two prefixes collide when they are equal OR one contains the other
    (`/provider` vs `/provider/x`) — in either case a request path could be
    served by both declarations, and "both" has no defensible answer."""
    return http_prefix_matches(a, b) or http_prefix_matches(b, a)


def _valid_http_prefix(prefix: str) -> str:
    if not prefix.startswith("/"):
        raise ValueError(f"http prefix {prefix!r} must start with '/'")
    if prefix == "/":
        raise ValueError(
            "http prefix '/' would claim the entire gateway root — declare at "
            "least one path segment"
        )
    if prefix.endswith("/"):
        raise ValueError(f"http prefix {prefix!r} must not end with a trailing '/'")
    segments = prefix[1:].split("/")
    for segment in segments:
        if not segment:
            raise ValueError(
                f"http prefix {prefix!r} has an empty path segment (a stray '//')"
            )
        if "{" in segment or "}" in segment:
            raise ValueError(
                f"http prefix {prefix!r} has a '{{param}}' segment {segment!r} — a "
                "prefix is LITERAL: it is matched against request paths, never "
                "templated"
            )
        if segment in {".", ".."}:
            raise ValueError(
                f"http prefix {prefix!r} contains a '.'/'..' segment — the proxy "
                "normalizes dot-segments away before matching, so this prefix "
                "could never be reached"
            )
        if not _ROUTE_LITERAL_RE.match(segment):
            raise ValueError(
                f"http prefix {prefix!r} has an invalid path segment {segment!r}"
            )
    if segments[0] in RESERVED_HTTP_PREFIXES:
        raise ValueError(
            f"http prefix {prefix!r} starts with reserved segment "
            f"{segments[0]!r} — the platform serves that path itself "
            f"(gateway.md §3a); reserved: {sorted(RESERVED_HTTP_PREFIXES)}"
        )
    return prefix


class HttpSurface(BaseModel):
    """One plain-HTTP contract this plugin serves, proxied by the gateway at
    the platform ROOT (gateway.md §3a).

    `extra="forbid"` — same fail-loud posture as `UIBlock`/`ReplicationBlock`:
    a typo'd key rejects the manifest rather than silently registering a
    surface that behaves differently than declared.
    """

    model_config = ConfigDict(extra="forbid")

    prefix: str = Field(
        description="root-level public path prefix, e.g. '/provider'. Literal "
        "segments only; the public path IS the plugin path (no rewriting), so "
        "the plugin must serve the SAME path it declares here"
    )
    methods: list[str] = Field(
        default_factory=lambda: ["GET"],
        description="the uppercase HTTP methods the gateway forwards under "
        "this prefix; anything else 405s at the gateway without a round-trip",
    )
    description: str | None = Field(
        default=None, description="human note — what contract this prefix serves"
    )

    _valid_prefix = field_validator("prefix")(_valid_http_prefix)

    @field_validator("methods")
    @classmethod
    def _valid_methods(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError(
                "http surface declares no methods — an empty list is a surface "
                "the gateway would never forward anything to"
            )
        unknown = [m for m in v if m not in HTTP_SURFACE_METHODS]
        if unknown:
            raise ValueError(
                f"unsupported http method(s) {unknown!r} — the gateway proxy "
                f"forwards {sorted(HTTP_SURFACE_METHODS)} (uppercase)"
            )
        dupes = sorted({m for m in v if v.count(m) > 1})
        if dupes:
            raise ValueError(f"duplicate http method(s): {dupes!r}")
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
    advertised_base_url: str | None = Field(
        default=None,
        description="optional peer-reachable address for this plugin's "
        "replication surfaces over the tailnet, when it differs from the "
        "loopback base_url advertised to this plugin's own registry (§4.1). "
        "Pairing (§5) prefers it; absent, pairing falls back to the "
        "port-preserving rewrite of base_url. None = no override (the "
        "fallback applies)",
    )

    @field_validator("advertised_base_url")
    @classmethod
    def _valid_advertised_base_url(cls, v: str | None) -> str | None:
        # It IS a base_url — the peer-facing one — so it shares
        # PluginManifest.base_url's shape rule (require an http(s) scheme, strip
        # a trailing slash; see `_valid_base_url`) and additionally forbids a
        # query or fragment, making it STRICTER than base_url. The extra rule is
        # load-bearing here and not on base_url: pairing uses this value VERBATIM
        # and then SUFFIXES the admin prefix / ingest_path onto it (§5), so
        # `http://h?x=1` or `http://h#f` would suffix into a corrupted URL
        # (`http://h?x=1/replication-admin`). A non-empty PATH is allowed on
        # purpose — a path-based serve front (§4.1) is a reason to declare this
        # field, and the ingest/admin suffix is meant to land under that path.
        if v is None:
            return v
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                f"advertised_base_url {v!r} must start with http:// or https://"
            )
        parts = urlsplit(v)
        if parts.query or parts.fragment:
            raise ValueError(
                f"advertised_base_url {v!r} must not carry a query or fragment — "
                "pairing suffixes the admin/ingest path onto it (§5), which a "
                "query or fragment would corrupt"
            )
        return v.rstrip("/")


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
    http: list[HttpSurface] = Field(
        default_factory=list,
        description="optional plain-HTTP contracts the gateway proxies at the "
        "platform root (gateway.md §3a), e.g. [{'prefix': '/provider', "
        "'methods': ['GET', 'POST']}]. Empty = the plugin serves nothing over "
        "the gateway but MCP/ui",
    )
    replication: ReplicationBlock | None = Field(
        default=None,
        description="optional replication contract declaration "
        "(replication-continuity.md §4); advisory metadata only — the "
        "gateway and health checker never read it. None if the plugin does "
        "not participate in replication",
    )

    @field_validator("http")
    @classmethod
    def _no_overlapping_http_prefixes(cls, v: list[HttpSurface]) -> list[HttpSurface]:
        """Within ONE manifest, no two prefixes may collide — equal, or one
        containing the other. Nesting (`/provider` + `/provider/x`) is rejected
        rather than resolved by longest-prefix: the resolver WOULD pick the
        deeper one, but then the two surfaces' `methods` lists silently
        disagree about the same request, which is an authoring mistake worth a
        422. (Across plugins the same collision rule is enforced at
        registration by `PluginRegistry.upsert` — there it's a 409, because
        the manifest itself is fine and the CONFLICT is with someone else.)"""
        for i, surface in enumerate(v):
            for other in v[i + 1 :]:
                if http_prefixes_collide(surface.prefix, other.prefix):
                    raise ValueError(
                        f"http prefixes {surface.prefix!r} and {other.prefix!r} "
                        "overlap within this manifest — a request path could "
                        "match both"
                    )
        return v

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
        if v in RESERVED_PLUGIN_NAMES:
            raise ValueError(
                f"plugin name {v!r} is reserved — it would shadow a shell "
                f"route (dashboard-ia.md §3); reserved: "
                f"{sorted(RESERVED_PLUGIN_NAMES)}"
            )
        return v

    @field_validator("base_url")
    @classmethod
    def _valid_base_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"base_url {v!r} must start with http:// or https://")
        return v.rstrip("/")
