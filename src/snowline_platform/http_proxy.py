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

**Root-level, VERBATIM.** Unlike `/ui-api/<plugin>/<path>` (which namespaces
by plugin and fixes the upstream prefix), an http surface's public path IS the
plugin path: `GET /provider/work-items?a=1&a=2` on the gateway becomes exactly
that request against `<base_url>`. The path is forwarded from the ASGI
`raw_path` — percent-encoding, trailing slash and all — and the query string
is forwarded as the raw bytes it arrived as, so an encoded identifier
(`%2F`, `%23`, `%25`), a route declared with a trailing slash, and a repeated
query key all reach the plugin unchanged. The only thing the proxy refuses is
a dot-segment (`.`/`..`) or an empty INTERIOR segment (`//`): those are not
normalized and forwarded (the `/ui-api` lesson — a normalized path can match
one prefix and land somewhere else) but simply fall through to the app's 404.
A consumer configured with the gateway's base URL (`MUSHER_PROVIDER_URL`)
therefore reaches the plugin's contract unchanged, and the address survives
the plugin moving ports or hosts.

Because prefixes are root-level they are a SHARED namespace: the manifest
reserves the platform's own top-level segments
(`manifest.RESERVED_HTTP_PREFIXES`, plus whatever the BUILT app actually
routes — `app.py` hands the registry the live set), and the registry refuses a
prefix another plugin already holds (409 at registration). By the time a
request reaches here, at most one plugin can claim it.

**Trust posture.** This path is NOT exempt from the trust middleware
(`middleware.TrustMiddleware`, `app.py`'s `exempt_paths={"/health"}`): a
proxied plugin contract rides the same tailnet CIDR gate as `/plugins` and
`/mcp`. The middleware wraps the whole app, above the router, so an untrusted
peer gets the gate's 403 before the router (and therefore this fallback) is
ever entered. The plugin's own bind stays loopback/tailnet — the gateway does
not make it public.

**Health-aware** (gateway.md §4): a plugin whose registry status is `DOWN`
short-circuits to 503 without a network round-trip; `UNKNOWN`/`UP` proceed —
the same routability rule `/ui-api` and `discover_upstreams` use.

**No retry.** A connect failure is a straight 502, matching `/ui-api`
exactly. Connect-phase retry (deploy-continuity.md §3) is the MCP gateway's
concern, where a tool call crossing a plugin redeploy is invisible to the
agent; an HTTP consumer sees the status code and owns its own retry policy, so
adding one here would invent a second policy for no gain.

**Header policy — a DENYLIST, the reverse-proxy norm.** Everything is
forwarded in both directions except: hop-by-hop headers (RFC 9110 §7.6.1),
`host` (the upstream must see its own; the gateway's is carried in
`X-Forwarded-Host`), `content-length` on the request (recomputed from the
buffered body — a client's can lie or be chunked), the caller's platform
credentials and cookies (`authorization`, `cookie` — not the plugin's
business), and any forwarding headers a caller invented (the gateway sets its
own `X-Forwarded-For/Host/Proto` and `X-Snowline-Gateway: 1`). A whitelist
would silently disable the mechanisms a contract consumer relies on —
`If-None-Match`/`If-Match` revalidation and optimistic concurrency,
`Idempotency-Key`, `Retry-After`, `Vary`. On the way back, `set-cookie` is
stripped (a plugin session must never be set on the gateway origin) and
`server`/`date` are left to uvicorn.

**Streaming, not buffering.** The upstream response is relayed as it arrives
(`aiter_raw`, so `content-encoding`/`content-length` stay truthful) — a
long-poll or event stream under a plugin prefix neither pins gateway memory
nor delays first byte to EOF. The request body is still buffered under the
same 64 KiB cap `/ui-api` uses (the contracts served here are small JSON).

**Redirects.** An absolute `Location`/`Content-Location` that points at the
plugin's own `base_url` (Starlette's redirect-slashes and `url_for` both
build those from the `Host` the upstream saw) is rewritten to the gateway
origin, so a redirect-following consumer never leaves the gated front door
for the plugin's private bind.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx
from fastapi import Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.routing import get_route_path
from starlette.types import ASGIApp, Receive, Scope, Send

from snowline_platform.manifest import HttpSurface
from snowline_platform.registry import PluginRegistry, PluginStatus, RegisteredPlugin

# The shared proxy client, the body cap and the capped-body reader are
# `/ui-api`'s (ui_api.py) — deliberately imported rather than re-derived: one
# httpx connection pool for both proxies (closed once by `aclose_client` in the
# app lifespan), one body limit, one enforcement loop.
from snowline_platform.ui_api import (
    POST_BODY_LIMIT,
    PROXY_TIMEOUT,
    _client,
    read_capped_body,
)

log = logging.getLogger("snowline_platform.http_proxy")

# Per-READ timeout for an upstream contract call. `/ui-api`'s PROXY_TIMEOUT
# (10 s) was sized for widget reads; a non-idempotent contract write that
# takes 12 s must not come back as "unreachable" after it applied (inviting a
# duplicate on retry). Connect still uses PROXY_TIMEOUT — a dead upstream is
# found just as fast.
UPSTREAM_READ_TIMEOUT = 60.0

HOP_BY_HOP_HEADERS: frozenset[str] = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})

# Stripped on the way UP (see the module docstring for why each).
STRIP_REQUEST_HEADERS: frozenset[str] = HOP_BY_HOP_HEADERS | {
    "host", "content-length",
    "authorization", "cookie",
    "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    "x-snowline-gateway",
}

# Stripped on the way BACK: hop-by-hop, the server framing uvicorn regenerates,
# and cookies (a plugin session on the gateway origin would be a confused-
# deputy hole).
STRIP_RESPONSE_HEADERS: frozenset[str] = HOP_BY_HOP_HEADERS | {
    "server", "date", "set-cookie",
}

# Response headers whose ABSOLUTE plugin-origin values are rewritten to the
# gateway origin.
REWRITE_ORIGIN_HEADERS: frozenset[str] = frozenset({"location", "content-location"})


class ProxyPathError(ValueError):
    """The request path cannot be expressed as an upstream URL (non-ASCII raw
    bytes, or httpx refusing the URL) — a 400, never a 500."""


def _well_formed(route_path: str) -> bool:
    """Only dot-segments and empty INTERIOR segments are refused — nothing is
    normalized. A trailing slash (an empty LAST segment) is a legitimate
    distinct path and is forwarded as-is."""
    segments = route_path.split("/")[1:]
    for i, seg in enumerate(segments):
        if seg in (".", ".."):
            return False
        if seg == "" and i != len(segments) - 1:
            return False
    return True


def _raw_route_path(scope: Scope) -> bytes:
    """The request path as the client sent it — percent-encoded, with the ASGI
    `root_path` (a platform served under a sub-path) stripped exactly as the
    router strips it for matching. Falls back to re-quoting the decoded path
    when a server did not supply `raw_path`."""
    raw = scope.get("raw_path")
    if raw is None:
        return quote(get_route_path(scope), safe="/:@!$&'()*+,;=~-._").encode("ascii")
    raw_str = raw.decode("latin-1")
    root = scope.get("root_path", "") or ""
    if root and raw_str.startswith(root):
        raw_str = raw_str[len(root):]
    try:
        return raw_str.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProxyPathError("request path carries non-ASCII raw bytes") from exc


def upstream_url(base_url: str, raw_path: bytes, query_string: bytes) -> httpx.URL:
    """`<base_url>` + the verbatim raw path and query. `base_url` may carry a
    path of its own (a plugin behind a sub-path) — it is kept; the manifest
    guarantees it carries no query or fragment. Raises `ProxyPathError` when
    httpx will not accept the result."""
    base = httpx.URL(base_url)
    path = base.raw_path.rstrip(b"/") + raw_path
    if query_string:
        path += b"?" + query_string
    try:
        return base.copy_with(raw_path=path)
    except (httpx.InvalidURL, ValueError) as exc:
        raise ProxyPathError(str(exc)) from exc


def _forward_headers(request: Request) -> list[tuple[str, str]]:
    out = [
        (name, value)
        for name, value in request.headers.items()
        if name not in STRIP_REQUEST_HEADERS
    ]
    # httpx would otherwise volunteer `accept-encoding: gzip, deflate` and the
    # upstream might compress for a caller that never asked; the raw stream
    # relays whatever comes back, so the caller's own preference (or none)
    # must be what the upstream sees.
    if "accept-encoding" not in request.headers:
        out.append(("accept-encoding", "identity"))
    # The DIRECT peer's IP (the same value the trust gate resolved on) — a
    # caller-supplied chain is stripped above so an upstream can't be fed one.
    if request.client is not None:
        out.append(("x-forwarded-for", request.client.host))
    if "host" in request.headers:
        out.append(("x-forwarded-host", request.headers["host"]))
    out.append(("x-forwarded-proto", request.url.scheme))
    out.append(("x-snowline-gateway", "1"))
    return out


def _rewrite_origin(value: str, plugin_base: str, gateway_origin: str) -> str:
    """An absolute URL at the plugin's own origin becomes the same path at the
    gateway origin; anything else (relative, or a genuinely external URL) is
    left alone."""
    base = plugin_base.rstrip("/")
    if value.lower().startswith(base.lower()) and (
        len(value) == len(base) or value[len(base)] in "/?#"
    ):
        return gateway_origin + value[len(base):]
    return value


def _response_headers(
    upstream: httpx.Response, plugin_base: str, gateway_origin: str
) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in upstream.headers.multi_items():
        lname = name.lower()
        if lname in STRIP_RESPONSE_HEADERS:
            continue
        if lname in REWRITE_ORIGIN_HEADERS:
            value = _rewrite_origin(value, plugin_base, gateway_origin)
        # A repeated header (`vary`, `link`) folds into one comma-joined value
        # — semantically identical for every header that survives the strip
        # (`set-cookie`, the one that is NOT foldable, never reaches here).
        out[lname] = f"{out[lname]}, {value}" if lname in out else value
    return out


async def _raw_body(upstream: httpx.Response):
    """The upstream body as it arrives on the wire (`aiter_raw` — no
    decompression, so the relayed `content-encoding`/`content-length` stay
    truthful). A response whose content was materialized before we saw it
    (an in-process transport handing back a pre-built `Response`) has no
    stream left to iterate; its bytes are relayed once instead."""
    if upstream.is_stream_consumed:
        yield upstream.content
        return
    async for chunk in upstream.aiter_raw():
        yield chunk


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

        # Match on the DECODED route path (`get_route_path` strips the ASGI
        # root_path exactly as the router's own matching does); forward the
        # RAW one. Nothing is normalized in between — a malformed path is
        # simply not ours.
        route_path = get_route_path(scope)
        if not _well_formed(route_path):
            await self._fallback(scope, receive, send)
            return

        match = registry.http_route(route_path)
        if match is None:
            await self._fallback(scope, receive, send)
            return
        entry, surface = match

        response = await self._proxy(request, entry, surface)
        await response(scope, receive, send)

    async def _proxy(
        self,
        request: Request,
        entry: RegisteredPlugin,
        surface: HttpSurface,
    ) -> Response:
        name = entry.manifest.name

        # Health-aware short-circuit (§4): DOWN never gets a network
        # round-trip. Checked BEFORE the (cheaper) method check, matching
        # `/ui-api`'s ordering of the same two guards — "the plugin is down"
        # is the more actionable answer while it is down.
        if entry.status is PluginStatus.DOWN:
            return JSONResponse(
                {"detail": f"plugin {name!r} is down"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if request.method not in surface.methods:
            allowed = ", ".join(sorted(surface.methods))
            return JSONResponse(
                {
                    "detail": f"{request.method} is not declared for "
                    f"{surface.prefix} by plugin {name!r} (declared: {allowed})"
                },
                status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
                headers={"allow": allowed},
            )

        try:
            target = upstream_url(
                entry.manifest.base_url,
                _raw_route_path(request.scope),
                request.scope.get("query_string", b""),
            )
        except ProxyPathError as exc:
            return JSONResponse(
                {"detail": f"request path cannot be forwarded: {exc}"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        body = await read_capped_body(request, POST_BODY_LIMIT)
        if body is None:
            return JSONResponse(
                {
                    "detail": f"request body exceeds the {POST_BODY_LIMIT}-byte "
                    "gateway proxy limit"
                },
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )

        client = _client(request.app)
        upstream_req = client.build_request(
            request.method,
            target,
            content=body,
            headers=_forward_headers(request),
            timeout=httpx.Timeout(PROXY_TIMEOUT, read=UPSTREAM_READ_TIMEOUT),
        )
        try:
            upstream_resp = await client.send(upstream_req, stream=True)
        except httpx.InvalidURL as exc:
            return JSONResponse(
                {"detail": f"request path cannot be forwarded: {exc}"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        except httpx.HTTPError as exc:
            log.warning(
                "http-proxy: plugin %r upstream %s unreachable: %s",
                name,
                target,
                exc,
            )
            return JSONResponse(
                {"detail": f"plugin {name!r} upstream unreachable: {exc}"},
                status_code=status.HTTP_502_BAD_GATEWAY,
            )

        gateway_origin = str(request.base_url).rstrip("/")
        return StreamingResponse(
            _raw_body(upstream_resp),
            status_code=upstream_resp.status_code,
            headers=_response_headers(
                upstream_resp, entry.manifest.base_url, gateway_origin
            ),
            background=BackgroundTask(upstream_resp.aclose),
        )
