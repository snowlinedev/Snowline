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

**GET only in v1** (§5) — POST is reserved for the §4.3 action contract, not
wired here yet; requesting any other verb 405s (FastAPI/Starlette's normal
method-not-allowed behavior for a route registered with one method).

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

from snowline_platform.registry import PluginNotFound, PluginRegistry, PluginStatus

log = logging.getLogger("snowline_platform.ui_api")

router = APIRouter(prefix="/ui-api", tags=["ui-api"])

# Bounded so a wedged plugin can't hang a shell request forever; the shell
# renders an error card on timeout same as any other upstream failure.
PROXY_TIMEOUT: float = 10.0


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
