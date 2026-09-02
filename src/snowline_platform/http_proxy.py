"""The gateway's plain-HTTP plugin proxy (gateway.md §3a).

A plugin may declare `http` surfaces in its manifest — root-level path
prefixes it serves over ordinary HTTP, beyond MCP and the `ui` block. This
module is the one thing that serves them.

**Wired as the router's FALLBACK, not as a catch-all route** (`app.py` sets
`app.router.default`). Starlette calls `default` only after every route has
failed to match AND after its redirect-slashes pass — so the proxy sees a
request only when nothing else in the app claimed it, and, crucially, the
`GET /mcp` → `307 /mcp/` redirect that MCP clients depend on still happens (a
literal `/{path:path}` catch-all route MATCHES `/mcp`, which suppresses that
redirect and breaks every client connecting to the bare `/mcp`). When no
plugin declares a matching prefix, the fallback delegates to the router's
ORIGINAL default, so an unknown path keeps its familiar `{"detail": "Not
Found"}` 404 rather than growing a proxy-flavored one.

**Root-level, no rewriting.** Unlike `/ui-api/<plugin>/<path>` (which
namespaces by plugin and fixes the upstream prefix), an http surface's public
path IS the plugin path: `GET /provider/work-items` on the gateway becomes
`GET <base_url>/provider/work-items` upstream, query string and all. That is
the whole point — a consumer configured with the gateway's base URL
(`MUSHER_PROVIDER_URL`) reaches the plugin's contract unchanged, and the
address survives the plugin moving ports or hosts.

Because prefixes are root-level they are a SHARED namespace: the manifest
reserves the platform's own top-level segments
(`manifest.RESERVED_HTTP_PREFIXES`) and the registry refuses a prefix another
plugin already holds (409 at registration). By the time a request reaches
here, at most one plugin can claim it.

**Trust posture.** This path is NOT exempt from the trust middleware
(`middleware.TrustMiddleware`, `app.py`'s `exempt_paths={"/health"}`): a
proxied plugin contract rides the same tailnet CIDR gate as `/plugins` and
`/mcp`. The middleware wraps the whole app, above the router, so an untrusted
peer gets the gate's 403 before the router (and therefore this fallback) is
ever entered. The plugin's own bind stays loopback/tailnet — the gateway does not
make it public.

**Health-aware** (gateway.md §4): a plugin whose registry status is `DOWN`
short-circuits to 503 without a network round-trip; `UNKNOWN`/`UP` proceed —
the same routability rule `/ui-api` and `discover_upstreams` use.

**No retry.** A connect failure is a straight 502, matching `/ui-api`
exactly. Connect-phase retry (deploy-continuity.md §3) is the MCP gateway's
concern, where a tool call crossing a plugin redeploy is invisible to the
agent; an HTTP consumer sees the status code and owns its own retry policy, so
adding one here would invent a second policy for no gain.

**Header hygiene.** Requests forward a WHITELIST (`content-type`, `accept`)
plus `X-Forwarded-For` and `X-Snowline-Gateway: 1`; `host`, `authorization`,
cookies and hop-by-hop headers are never forwarded — a plugin must not be able
to read the caller's platform credentials, and a forwarded `host` would break
upstream absolute-URL generation. Responses come back through a whitelist too
(`content-type`, `cache-control`, `etag`, `location`), so hop-by-hop and
transport-framing headers can't leak across the proxy.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.routing import get_route_path
from starlette.types import ASGIApp, Receive, Scope, Send

from snowline_platform.manifest import HttpSurface
from snowline_platform.registry import PluginRegistry, PluginStatus, RegisteredPlugin

# The shared proxy client + the path-normalization and body-cap rules are
# `/ui-api`'s (ui_api.py) — deliberately imported rather than re-derived: one
# httpx connection pool for both proxies (closed once by `aclose_client` in the
# app lifespan), one traversal-normalization implementation, one body limit.
from snowline_platform.ui_api import POST_BODY_LIMIT, _client, _safe_upstream_suffix

log = logging.getLogger("snowline_platform.http_proxy")

# The methods the proxy forwards — the same set a manifest may declare
# (`manifest.HTTP_SURFACE_METHODS`, pinned equal by a test). A method outside
# it can never appear in a surface's `methods`, so it is refused by the same
# 405 guard as an undeclared one; nothing here needs a second list.
PROXY_METHODS: frozenset[str] = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

# Request headers forwarded upstream. Everything else is dropped — notably
# `host` (upstream must see its own), `authorization`/`cookie` (the caller's
# platform credentials are not the plugin's business), and every hop-by-hop
# header. `content-length` is NOT forwarded: the body is buffered here, so
# httpx sets the true length from what we actually send — a client's header
# could be a lie or absent (chunked).
FORWARD_REQUEST_HEADERS: frozenset[str] = frozenset({"content-type", "accept"})

# Response headers passed back to the caller. `content-length` is deliberately
# absent: httpx transparently decompresses the upstream body, so the upstream
# length can describe bytes we are not returning — Starlette computes the
# correct one from the body we hand it.
FORWARD_RESPONSE_HEADERS: frozenset[str] = frozenset(
    {"content-type", "cache-control", "etag", "location"}
)


def _forward_headers(request: Request) -> dict[str, str]:
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in FORWARD_REQUEST_HEADERS
    }
    # The DIRECT peer's IP (the same value the trust gate resolved on) — the
    # inbound X-Forwarded-For is not forwarded, so an upstream can't be fed a
    # chain a caller made up.
    if request.client is not None:
        headers["x-forwarded-for"] = request.client.host
    headers["x-snowline-gateway"] = "1"
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() in FORWARD_RESPONSE_HEADERS
    }


async def _read_capped_body(request: Request) -> bytes | None:
    """The request body, or `None` if it exceeds `POST_BODY_LIMIT`.

    Same ONE enforcement path as `/ui-api`'s POST: checked against the actual
    streamed bytes (a Content-Length header can lie, be absent, or
    chunk-encode) and BEFORE buffering each chunk, so an oversize body never
    occupies memory past the limit."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > POST_BODY_LIMIT:
            return None
        body.extend(chunk)
    return bytes(body)


class PluginHttpProxy:
    """The router fallback that proxies a request to whichever plugin declared
    an `http` prefix covering it (gateway.md §3a).

    Constructed with the router's ORIGINAL `default` handler and installed as
    the new one (`app.py`). Starlette reaches `default` only after no route
    matched and its redirect-slashes pass found nothing, so:

      * every platform route — `/health`, `/plugins`, `/ui-api/…`, the MCP
        mounts — is matched first and can never be shadowed by a plugin prefix;
      * `GET /mcp` still 307s to the `/mcp/` mount (a literal catch-all ROUTE
        would match `/mcp` and swallow that redirect);
      * a path no plugin claims falls through to the original 404, so unknown
        paths keep the exact response they had before this proxy existed.
    """

    def __init__(self, fallback: ASGIApp) -> None:
        self._fallback = fallback

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Websockets (and anything else) keep the router's own behavior —
            # the proxy speaks HTTP only.
            await self._fallback(scope, receive, send)
            return

        request = Request(scope, receive)
        registry: PluginRegistry = request.app.state.registry

        # Normalize dot-segments BEFORE matching, not after, so the path
        # matched against the declared prefix is byte-identical to the path
        # forwarded upstream (the `/ui-api` lesson: matching a
        # pre-normalization string lets a request "match" one prefix while
        # landing somewhere else). A path that climbs out of every declared
        # prefix simply resolves to no plugin — it cannot reach a platform
        # route this way either, since normalization happens after routing.
        # `get_route_path`, not `scope["path"]`: it strips the ASGI
        # `root_path` exactly as the router's own matching does, so a platform
        # served under a sub-path compares the same string routing compared.
        suffix = _safe_upstream_suffix(get_route_path(scope).lstrip("/"))
        if suffix is None:
            await self._fallback(scope, receive, send)
            return
        path = f"/{suffix}"

        match = registry.http_route(path)
        if match is None:
            await self._fallback(scope, receive, send)
            return
        entry, surface = match

        response = await self._proxy(request, entry, surface, path)
        await response(scope, receive, send)

    async def _proxy(
        self,
        request: Request,
        entry: RegisteredPlugin,
        surface: HttpSurface,
        path: str,
    ) -> Response:
        name = entry.manifest.name

        # Health-aware short-circuit (§4): DOWN never gets a network
        # round-trip. Checked BEFORE the (cheaper) method check, matching
        # `/ui-api`'s ordering of the same two guards — "the plugin is down"
        # is the more actionable answer while it is down, and the two proxies
        # answering in a different order would be a gratuitous difference.
        if entry.status is PluginStatus.DOWN:
            return JSONResponse(
                {"detail": f"plugin {name!r} is down"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if request.method not in surface.methods:
            return JSONResponse(
                {
                    "detail": f"{request.method} is not declared for "
                    f"{surface.prefix} by plugin {name!r} (declared: "
                    f"{sorted(surface.methods)})"
                },
                status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            )

        body = await _read_capped_body(request)
        if body is None:
            return JSONResponse(
                {
                    "detail": f"request body exceeds the {POST_BODY_LIMIT}-byte "
                    "gateway proxy limit"
                },
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )

        # Root-level: the public path IS the plugin path, appended to base_url
        # verbatim.
        upstream_url = f"{entry.manifest.base_url}{path}"
        client = _client(request.app)
        try:
            upstream_resp = await client.request(
                request.method,
                upstream_url,
                params=request.query_params,
                content=body,
                headers=_forward_headers(request),
            )
        except httpx.HTTPError as exc:
            log.warning(
                "http-proxy: plugin %r upstream %s unreachable: %s",
                name,
                upstream_url,
                exc,
            )
            return JSONResponse(
                {"detail": f"plugin {name!r} upstream unreachable: {exc}"},
                status_code=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=_response_headers(upstream_resp),
        )
