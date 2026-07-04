"""The `/ui-api` data-plane proxy (ui-shell.md §5).

`GET /ui-api/<plugin>/<path>` proxies to `<plugin base_url>/ui-api/<path>` —
the shell's ONLY way to reach plugin data, so the browser talks to one origin
(the platform), the tailnet CIDR gate covers everything, and no CORS is ever
needed (identical over localhost and tailnet, per the SSH-into-host daily
flow).

**Path allowlist, structurally.** The `/ui-api` prefix is FIXED on the
upstream side: we always emit `<base_url>/ui-api/<path>`, never
`<base_url>/<path>`, so the proxy cannot be pointed at a plugin's `/mcp` or any
other route regardless of what a manifest's `data`/`endpoint` value or the
request path contains (the manifest side ALSO checks `data` starts with
`/ui-api/` at registration — `manifest.py`'s `_valid_ui_data` — belt +
suspenders). `path` is additionally normalized with `posixpath.normpath`
against a synthetic root before being appended, so a `..`-laden request
(`/ui-api/gov/../../mcp`) can climb no higher than that synthetic root — which
is INSIDE `/ui-api/`, prepended after normalization, not before — so it can
never resolve outside the plugin's `/ui-api` namespace.

**Health-aware.** A plugin whose registry `status` is `DOWN` short-circuits to
503 (§5) — the shell renders its grey state — instead of waiting out a dead
upstream; `UNKNOWN`/`UP` proceed (same routability rule the gateway's
`discover_upstreams` uses).

**POST, allowlisted structurally** (§5, activated by
`shadow-conversations.md` §3): unlike GET — which proxies any path under a
plugin's `/ui-api` namespace — a POST is only forwarded when the CONCRETE
path matches an endpoint the plugin DECLARED as a write target in its
manifest `ui` block (today: `UIPage.composer.endpoint` on `thread` pages;
`actions[].endpoint` shares the posture per spec but isn't modeled in the
manifest yet, so there is nothing to collect from it until it lands — see
`_declared_write_templates`). Matching is per path-segment: a `{param}`
template segment matches exactly one non-empty concrete segment, everything
else must match literally, and segment COUNT must match — so a template
can't greedily swallow extra segments. Matching runs against the SAME
`_safe_upstream_suffix`-normalized path that gets forwarded (normalize
before matching, not after) — matching the raw pre-normalization path would
let a dot-segment (`/branches/../messages`) satisfy a `{param}` slot with a
literal `..` while the actual (normalized) upstream request lands somewhere
else entirely, silently defeating the allowlist. An unmatched path 404s
(unknown plugin, or a path that can't be normalized) or 403s (plugin known,
path not declared) — it can never reach an undeclared route, another plugin,
or an MCP surface, the same posture the GET path allowlist gives by
construction. The body must be `application/json`
(415 otherwise) and is size-capped at `POST_BODY_LIMIT` bytes (413 otherwise,
checked against a lying/absent Content-Length AND enforced while actually
reading the stream) — forwarded verbatim, never interpreted, per
`shadow-conversations.md` §3.

**No retry here.** A connect failure is a straight 502 — retry-on-transient is
the GATEWAY's concern (gateway.py's connect-phase retry, issue #58); the shell
renders an error card on a 502, so retrying here would just be double
insurance for a problem already owned elsewhere.
"""

from __future__ import annotations

import logging
import posixpath

import httpx
from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from snowline_platform.registry import (
    PluginNotFound,
    PluginRegistry,
    PluginStatus,
    RegisteredPlugin,
)

log = logging.getLogger("snowline_platform.ui_api")

router = APIRouter(prefix="/ui-api", tags=["ui-api"])

# Bounded so a wedged plugin can't hang a shell request forever; the shell
# renders an error card on timeout same as any other upstream failure.
PROXY_TIMEOUT: float = 10.0

# 64 KiB — a conversation message, not an upload (shadow-conversations.md §3).
POST_BODY_LIMIT: int = 64 * 1024


def _safe_upstream_suffix(path: str) -> str | None:
    """Collapse `path`'s dot-segments against a SYNTHETIC root, returning the
    normalized plugin-relative suffix (no leading '/') or `None` if it still
    tries to climb above that root.

    The synthetic root is `/` — NOT `/ui-api` — deliberately: normalizing
    first and prepending `/ui-api/` after means the prefix itself is never
    part of what a `..` could climb out of. `posixpath.normpath` already
    collapses excess leading `..` above `/` down to `/` (you cannot go above
    root), so in practice this can't return `None` for any string reachable
    through a URL path — the explicit check is defense in depth, not the
    only line of defense.
    """
    normalized = posixpath.normpath(f"/{path}")
    if normalized == "/":
        return ""
    if normalized == "/.." or normalized.startswith("/../"):
        return None
    return normalized[1:]


def _client(app: FastAPI) -> httpx.AsyncClient:
    """The shared proxy client, created lazily on first use and cached on
    `app.state` — never one client per request. Closed at shutdown by
    `aclose_client` (wired into the app lifespan)."""
    client = getattr(app.state, "ui_api_client", None)
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(PROXY_TIMEOUT))
        app.state.ui_api_client = client
    return client


async def aclose_client(app: FastAPI) -> None:
    """Close the shared client if one was ever created. Safe to call even when
    no `/ui-api` request was ever served (the attribute is just absent)."""
    client = getattr(app.state, "ui_api_client", None)
    if client is not None:
        await client.aclose()
        app.state.ui_api_client = None


def _declared_write_templates(entry: RegisteredPlugin) -> list[list[str]]:
    """The plugin-relative path SEGMENTS of every endpoint this plugin
    declared as a POST write target in its manifest `ui` block.

    Today that's `composer.endpoint` on `thread` pages (`shadow-
    conversations.md` §4). `actions[].endpoint` shares the same posture per
    spec (§3/§4.3), but `actions` isn't modeled on `UIPage`/`UIWidget` yet —
    only reserved in the SDK's documentation constants — so there is nothing
    to collect from it until a later PR adds the field; this function is
    written to make that a pure addition (one more comprehension), not a
    matching-logic change.

    Each endpoint was already validated at registration to start with
    `/ui-api/` (`manifest.py`'s `_valid_ui_endpoint`), so stripping that
    fixed prefix and splitting on `/` gives the same segment shape `path`
    arrives in (no leading slash).
    """
    manifest = entry.manifest
    if manifest.ui is None:
        return []
    return [
        page.composer.endpoint[len("/ui-api/") :].split("/")
        for page in manifest.ui.pages
        if page.composer is not None
    ]


def _segments_match(path_segments: list[str], template_segments: list[str]) -> bool:
    """One template from `_declared_write_templates` against the concrete
    request path's segments — segment COUNT must match (a template can't
    greedily swallow extra segments) and each `{param}` template segment
    matches exactly one NON-EMPTY concrete segment; every other segment must
    match literally."""
    if len(path_segments) != len(template_segments):
        return False
    for concrete, template in zip(path_segments, template_segments):
        if template.startswith("{") and template.endswith("}"):
            if not concrete:
                return False
            continue
        if concrete != template:
            return False
    return True


def _is_declared_write_path(entry: RegisteredPlugin, path: str) -> bool:
    path_segments = path.split("/")
    return any(
        _segments_match(path_segments, template)
        for template in _declared_write_templates(entry)
    )


@router.get("/{plugin}/{path:path}")
async def proxy(plugin: str, path: str, request: Request) -> Response:
    registry: PluginRegistry = request.app.state.registry
    try:
        entry = registry.get(plugin)
    except PluginNotFound:
        return JSONResponse(
            {"detail": f"plugin {plugin!r} is not registered"},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # Health-aware short-circuit (§5): DOWN never gets a network round-trip.
    if entry.status is PluginStatus.DOWN:
        return JSONResponse(
            {"detail": f"plugin {plugin!r} is down"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    suffix = _safe_upstream_suffix(path)
    if suffix is None:
        return JSONResponse(
            {"detail": f"invalid /ui-api path {path!r}"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    upstream_url = f"{entry.manifest.base_url}/ui-api/{suffix}"

    client = _client(request.app)
    try:
        upstream_resp = await client.get(
            upstream_url, params=request.query_params
        )
    except httpx.HTTPError as exc:
        log.warning(
            "ui-api: plugin %r upstream %s unreachable: %s",
            plugin,
            upstream_url,
            exc,
        )
        return JSONResponse(
            {"detail": f"plugin {plugin!r} upstream unreachable: {exc}"},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type"),
    )


@router.post("/{plugin}/{path:path}")
async def proxy_post(plugin: str, path: str, request: Request) -> Response:
    registry: PluginRegistry = request.app.state.registry
    try:
        entry = registry.get(plugin)
    except PluginNotFound:
        return JSONResponse(
            {"detail": f"plugin {plugin!r} is not registered"},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # Health-aware short-circuit (§5): DOWN never gets a network round-trip —
    # same rule, checked before the (cheaper) allowlist check, as the GET path.
    if entry.status is PluginStatus.DOWN:
        return JSONResponse(
            {"detail": f"plugin {plugin!r} is down"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Normalize BEFORE the allowlist check — not after — so the match is
    # against the exact string that ends up in the upstream URL below. A
    # dot-segment path (`/branches/../messages`) would otherwise let a
    # `{param}` slot "match" a literal `..` pre-normalization while the
    # actual forwarded request (built from the post-normalization suffix)
    # lands on a completely different, undeclared upstream path — silently
    # defeating the structural allowlist this route exists to enforce.
    suffix = _safe_upstream_suffix(path)
    if suffix is None:
        return JSONResponse(
            {"detail": f"invalid /ui-api path {path!r}"},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # Structural allowlist: the concrete (normalized) path must match an
    # endpoint the plugin DECLARED as a write target. Unlike GET, POST has no
    # open passthrough under /ui-api/ — an undeclared path 403s.
    if not _is_declared_write_path(entry, suffix):
        return JSONResponse(
            {
                "detail": f"POST /ui-api/{plugin}/{path} is not a declared "
                "write endpoint"
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return JSONResponse(
            {
                "detail": f"unsupported content-type {content_type!r} — "
                "POST /ui-api requires application/json"
            },
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    def _too_large() -> JSONResponse:
        return JSONResponse(
            {
                "detail": f"request body exceeds the {POST_BODY_LIMIT}-byte "
                "/ui-api POST limit"
            },
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )

    # ONE enforcement path: the cap is checked against the actual streamed
    # bytes (a Content-Length header can lie, be absent, or chunk-encode), and
    # BEFORE buffering each chunk, so an oversize single-chunk body never
    # occupies memory past the limit.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > POST_BODY_LIMIT:
            return _too_large()
        body.extend(chunk)
    body_bytes = bytes(body)

    upstream_url = f"{entry.manifest.base_url}/ui-api/{suffix}"

    client = _client(request.app)
    try:
        upstream_resp = await client.post(
            upstream_url,
            params=request.query_params,
            content=body_bytes,
            headers={"content-type": "application/json"},
        )
    except httpx.HTTPError as exc:
        log.warning(
            "ui-api: plugin %r upstream %s unreachable: %s",
            plugin,
            upstream_url,
            exc,
        )
        return JSONResponse(
            {"detail": f"plugin {plugin!r} upstream unreachable: {exc}"},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type"),
    )
