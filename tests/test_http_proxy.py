"""The gateway's plain-HTTP plugin proxy (gateway.md §3a).

Same seam as the `/ui-api` proxy tests: `ui_api._client` lazily creates and
caches an `httpx.AsyncClient` on `app.state.ui_api_client`, and BOTH proxies
share it (one connection pool, one shutdown path) — so pre-seeding that
attribute with a `MockTransport` stands in for a live plugin's HTTP surface
here too, without a socket.

The proxy's structural claim — that it cannot shadow a platform route — is
tested three ways: behaviorally (platform routes still answer while a plugin
holds a look-alike prefix), on the specific case a catch-all ROUTE would have
broken (`GET /mcp`'s redirect to the mount), and structurally
(`test_reserved_prefixes_cover_every_app_route` walks the BUILT app).
"""

from __future__ import annotations

import httpx
import pytest
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from snowline_platform import http_proxy, manifest as manifest_mod
from snowline_platform.app import create_app
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry, PluginStatus
from snowline_platform.trust import Principal, TrustResolver
from snowline_platform.ui_api import POST_BODY_LIMIT


class _AlwaysTrust:
    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _registry(
    *prefixes: str,
    methods: list[str] | None = None,
    status: PluginStatus | None = None,
) -> PluginRegistry:
    reg = PluginRegistry()
    reg.upsert(
        PluginManifest(
            name="pm",
            base_url="http://pm-host:8802",
            http=[
                {"prefix": p, "methods": methods or ["GET", "POST"]}
                for p in (prefixes or ("/provider",))
            ],
        )
    )
    if status is not None:
        reg.set_status("pm", status)
    return reg


def _app(registry: PluginRegistry | None = None, resolver=None):
    return create_app(
        resolver=resolver or TrustResolver([_AlwaysTrust()]),
        registry=registry if registry is not None else _registry(),
        migrate_on_startup=False,
    )


def _wire_mock_upstream(app, handler) -> None:
    app.state.ui_api_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )


# --- happy path -------------------------------------------------------------


def test_get_forwards_path_and_query_verbatim_to_the_plugin():
    app = _app()

    def handler(request: httpx.Request) -> httpx.Response:
        # Root-level: the PUBLIC path is the PLUGIN path — no rewriting, no
        # plugin-name segment (unlike /ui-api).
        assert request.url.host == "pm-host"
        assert request.url.path == "/provider/work-items"
        assert request.url.params["state"] == "queued"
        return httpx.Response(200, json={"items": []})

    _wire_mock_upstream(app, handler)
    r = TestClient(app).get("/provider/work-items?state=queued")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"items": []}


def test_post_forwards_body_and_content_type_and_passes_status_through():
    app = _app()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/provider/work-items/abc/dispatched"
        assert request.headers["content-type"] == "application/json"
        assert request.content == b'{"agent": "musher"}'
        return httpx.Response(202, json={"ok": True})

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(
        "/provider/work-items/abc/dispatched",
        content=b'{"agent": "musher"}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 202
    assert r.json() == {"ok": True}


def test_the_prefix_itself_resolves_not_only_paths_under_it():
    app = _app()
    _wire_mock_upstream(
        app,
        lambda r: httpx.Response(200, json={"path": r.url.path}),
    )
    r = TestClient(app).get("/provider")
    assert r.status_code == 200
    assert r.json() == {"path": "/provider"}


def test_proxy_shares_the_ui_api_client_across_both_proxies():
    app = _app()
    _wire_mock_upstream(app, lambda r: httpx.Response(200, json={}))
    client = TestClient(app)
    seeded = app.state.ui_api_client
    assert client.get("/provider/a").status_code == 200
    assert client.get("/provider/b").status_code == 200
    # No second pool was created — the one client serves both proxies.
    assert app.state.ui_api_client is seeded


# --- refusals ---------------------------------------------------------------


def test_unclaimed_path_keeps_the_apps_own_404():
    # The proxy is the router's FALLBACK and delegates to the original default
    # when no plugin claims the path — so an unknown path answers exactly as it
    # did before this proxy existed, rather than growing a proxy-flavored 404.
    app = _app()
    r = TestClient(app).get("/nobody/home")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


def test_segment_aligned_lookalike_path_is_404_not_proxied():
    app = _app()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("'/providerx' must not resolve to '/provider'")

    _wire_mock_upstream(app, handler)
    assert TestClient(app).get("/providerx/work-items").status_code == 404


def test_undeclared_method_is_405_without_a_round_trip():
    app = _app(_registry(methods=["GET"]))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an undeclared method must not reach the upstream")

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post("/provider/work-items", json={})
    assert r.status_code == 405
    assert "/provider" in r.json()["detail"]


@pytest.mark.parametrize("verb", ["head", "options"])
def test_methods_outside_the_declarable_set_are_405(verb):
    # A manifest can only declare the five proxied methods, so HEAD/OPTIONS on
    # a claimed prefix hit the same "not declared" guard — the path exists, the
    # method doesn't, which is a 405 rather than a 404.
    app = _app()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an undeclared method must not reach the upstream")

    _wire_mock_upstream(app, handler)
    assert getattr(TestClient(app), verb)("/provider/work-items").status_code == 405


def test_down_plugin_is_503_without_a_network_call():
    app = _app(_registry(status=PluginStatus.DOWN))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the upstream for a DOWN plugin")

    _wire_mock_upstream(app, handler)
    r = TestClient(app).get("/provider/work-items")
    assert r.status_code == 503
    assert "pm" in r.json()["detail"]


def test_unknown_and_up_statuses_proceed():
    # Only an explicit DOWN short-circuits — same routability rule as /ui-api
    # and the MCP gateway's discover_upstreams.
    for st in (None, PluginStatus.UP):
        app = _app(_registry(status=st))
        _wire_mock_upstream(app, lambda r: httpx.Response(200, json={}))
        assert TestClient(app).get("/provider/x").status_code == 200


def test_unreachable_upstream_is_502():
    app = _app()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _wire_mock_upstream(app, handler)
    r = TestClient(app).get("/provider/work-items")
    assert r.status_code == 502
    assert "pm" in r.json()["detail"]


def test_oversize_body_is_413_without_a_round_trip():
    app = _app()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an oversize body must not reach the upstream")

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(
        "/provider/work-items",
        content=b"x" * (POST_BODY_LIMIT + 1),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413


def test_body_at_the_cap_is_forwarded():
    app = _app()
    payload = b"x" * POST_BODY_LIMIT

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == payload
        return httpx.Response(200, json={})

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(
        "/provider/work-items",
        content=payload,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200


# --- header hygiene ---------------------------------------------------------


def test_request_headers_are_whitelisted_and_forwarding_headers_added():
    app = _app()

    def handler(request: httpx.Request) -> httpx.Response:
        # The caller's platform credentials and cookies are NOT the plugin's
        # business, and the upstream must see its OWN host.
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.headers["host"] == "pm-host:8802"
        # Whitelisted through.
        assert request.headers["accept"] == "application/json"
        # Added by the gateway.
        assert request.headers["x-forwarded-for"] == "testclient"
        assert request.headers["x-snowline-gateway"] == "1"
        return httpx.Response(200, json={})

    _wire_mock_upstream(app, handler)
    r = TestClient(app).get(
        "/provider/work-items",
        headers={
            "authorization": "Bearer platform-secret",
            "cookie": "session=abc",
            "accept": "application/json",
            "x-forwarded-for": "203.0.113.7",  # a caller-invented chain
        },
    )
    assert r.status_code == 200


def test_response_headers_are_whitelisted():
    app = _app()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={},
            headers={
                "etag": '"v1"',
                "cache-control": "no-store",
                "set-cookie": "plugin_session=abc",
                "x-plugin-internal": "leak",
            },
        )

    _wire_mock_upstream(app, handler)
    r = TestClient(app).get("/provider/work-items")
    assert r.headers["etag"] == '"v1"'
    assert r.headers["cache-control"] == "no-store"
    assert "set-cookie" not in r.headers
    assert "x-plugin-internal" not in r.headers


def test_redirect_location_is_passed_through_unfollowed():
    app = _app()
    _wire_mock_upstream(
        app,
        lambda r: httpx.Response(303, headers={"location": "/provider/work-items/1"}),
    )
    r = TestClient(app).get("/provider/work-items", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/provider/work-items/1"


# --- the catch-all cannot shadow the platform -------------------------------


def test_platform_routes_still_answer_with_a_lookalike_prefix_registered():
    # '/pluginsx' passes the reserved check (segment-aligned: it is not
    # '/plugins'), so a plugin CAN hold it — and every platform route must
    # still reach its own handler, because the catch-all is registered last.
    app = _app(_registry("/pluginsx", "/provider"))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"platform route leaked to the proxy: {request.url.path}")

    _wire_mock_upstream(app, handler)
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    assert "plugins" in client.get("/plugins").json()
    assert client.get("/whoami").json()["id"] == "test-owner"
    # /ui-api's own 404 shape (its plugin lookup), not the proxy's.
    r = client.get("/ui-api/ghost/pages")
    assert r.status_code == 404 and "ghost" in r.json()["detail"]


def test_bare_mcp_still_redirects_to_the_mount():
    """The regression a catch-all ROUTE would have caused: Starlette answers
    `GET /mcp` (a Mount at `/mcp`) with a 307 to `/mcp/` from its
    redirect-slashes pass, which runs BEFORE the router's default. A
    `/{path:path}` route matches `/mcp` outright and suppresses that redirect
    — silently breaking every MCP client pointed at the bare `/mcp`. As a
    fallback, the proxy never sees it."""
    app = _app(_registry("/pluginsx", "/provider"))
    _wire_mock_upstream(
        app, lambda r: pytest.fail(f"MCP mount leaked to the proxy: {r.url.path}")
    )
    r = TestClient(app).get("/mcp", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith("/mcp/")


def test_traversal_cannot_reach_a_plugin_prefix_it_did_not_declare():
    app = _app()

    def handler(request: httpx.Request) -> httpx.Response:
        # Normalization happens BEFORE the prefix match, so what matched is
        # exactly what is forwarded.
        assert request.url.path == "/provider/work-items"
        return httpx.Response(200, json={})

    _wire_mock_upstream(app, handler)
    client = TestClient(app)
    # Percent-encoded dot-segments survive the client and reach the route.
    assert client.get("/provider/x/%2e%2e/work-items").status_code == 200
    # Climbing OUT of the declared prefix resolves to no plugin, rather than
    # matching '/provider' and forwarding somewhere else — including when the
    # climb lands on a platform path (normalization happens after routing, so
    # this is a 404, never a proxied or re-routed request).
    assert client.get("/provider/%2e%2e/plugins").json() == {"detail": "Not Found"}
    assert client.get("/provider/%2e%2e/nope").status_code == 404


def test_trust_gate_still_applies_to_a_proxied_path():
    # The proxy route is NOT in the middleware's exempt set (only /health is):
    # an untrusted peer gets the gate's 403 before the handler runs.
    app = _app(resolver=TrustResolver([]))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an untrusted request must never reach a plugin")

    _wire_mock_upstream(app, handler)
    client = TestClient(app)
    r = client.get("/provider/work-items")
    assert r.status_code == 403
    assert r.json() == {"detail": "untrusted source"}
    # /health stays exempt, so the gate's scope is unchanged by this route.
    assert client.get("/health").status_code == 200


def test_reserved_prefixes_cover_every_app_route():
    """Structural guard: every top-level path segment the BUILT app serves must
    be in `RESERVED_HTTP_PREFIXES`, so a future platform route cannot be added
    without either reserving its segment or consciously leaving it claimable by
    a plugin (which would silently shadow it — the route would win and the
    plugin's surface would be dead)."""
    app = _app(PluginRegistry())
    unreserved = set()
    for route in app.routes:
        if not isinstance(route, (Route, Mount)):
            continue
        path = route.path
        first = path.lstrip("/").split("/")[0]
        if not first:  # the bare "/" (nothing serves it today)
            continue
        if first not in manifest_mod.RESERVED_HTTP_PREFIXES:
            unreserved.add(first)
    assert not unreserved, (
        f"platform routes whose top-level segment is unreserved: "
        f"{sorted(unreserved)} — add them to manifest.RESERVED_HTTP_PREFIXES"
    )


def test_proxy_methods_match_the_manifest_vocabulary():
    # The set a manifest may declare and the set the proxy forwards are the
    # same set; they must never drift (a manifest-declarable method the proxy
    # dropped would 405 at runtime with a valid manifest).
    assert http_proxy.PROXY_METHODS == manifest_mod.HTTP_SURFACE_METHODS
