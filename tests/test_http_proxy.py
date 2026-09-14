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


def test_request_headers_denylist_strips_credentials_and_adds_forwarding():
    app = _app()

    def handler(request: httpx.Request) -> httpx.Response:
        # The caller's platform credentials and cookies are NOT the plugin's
        # business, and the upstream must see its OWN host.
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.headers["host"] == "pm-host:8802"
        # Everything else rides through — the mechanisms a contract consumer
        # relies on are not the gateway's to disable.
        assert request.headers["accept"] == "application/json"
        assert request.headers["if-none-match"] == '"v1"'
        assert request.headers["idempotency-key"] == "abc-123"
        # Added by the gateway; the caller-invented chain is replaced.
        assert request.headers["x-forwarded-for"] == "testclient"
        assert request.headers["x-forwarded-host"] == "testserver"
        assert request.headers["x-forwarded-proto"] == "http"
        assert request.headers["x-snowline-gateway"] == "1"
        # The caller's own encoding preference, not httpx's default.
        assert request.headers["accept-encoding"] == "br"
        return httpx.Response(200, json={})

    _wire_mock_upstream(app, handler)
    r = TestClient(app).get(
        "/provider/work-items",
        headers={
            "authorization": "Bearer platform-secret",
            "cookie": "session=abc",
            "accept": "application/json",
            "if-none-match": '"v1"',
            "idempotency-key": "abc-123",
            "x-forwarded-for": "203.0.113.7",  # a caller-invented chain
            "x-snowline-gateway": "0",
            "accept-encoding": "br",
        },
    )
    assert r.status_code == 200


def test_no_accept_encoding_from_the_caller_means_identity_upstream():
    """httpx would otherwise volunteer `gzip, deflate`; the raw relay would then
    hand a compressed body to a caller that never asked for one."""
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/provider/x", "headers": [],
             "query_string": b"", "scheme": "http", "client": ("1.2.3.4", 5)}
    headers = dict(http_proxy._forward_headers(Request(scope)))
    assert headers["accept-encoding"] == "identity"
    assert headers["x-forwarded-for"] == "1.2.3.4"


def test_response_headers_denylist_passes_contract_headers_and_strips_cookies():
    app = _app()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={},
            headers={
                "etag": '"v1"',
                "cache-control": "no-store",
                "retry-after": "5",
                "vary": "Accept",
                "set-cookie": "plugin_session=abc",
                "x-plugin-internal": "fine-to-see",
            },
        )

    _wire_mock_upstream(app, handler)
    r = TestClient(app).get("/provider/work-items")
    assert r.headers["etag"] == '"v1"'
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["retry-after"] == "5"
    assert r.headers["vary"] == "Accept"
    assert r.headers["x-plugin-internal"] == "fine-to-see"
    assert "set-cookie" not in r.headers
    assert r.json() == {}


def test_relative_redirect_location_is_passed_through_unfollowed():
    app = _app()
    _wire_mock_upstream(
        app,
        lambda r: httpx.Response(303, headers={"location": "/provider/work-items/1"}),
    )
    r = TestClient(app).get("/provider/work-items", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/provider/work-items/1"


def test_absolute_redirect_at_the_plugin_origin_is_rewritten_to_the_gateway():
    """Starlette's redirect-slashes / url_for build absolute Locations from the
    Host the upstream saw (its own); a following consumer must stay behind the
    gateway, never be sent to the plugin's private bind."""
    app = _app()
    _wire_mock_upstream(
        app,
        lambda r: httpx.Response(
            307, headers={"location": "http://pm-host:8802/provider/work-items/"}
        ),
    )
    r = TestClient(app).get("/provider/work-items", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "http://testserver/provider/work-items/"


def test_external_absolute_redirect_is_left_alone():
    app = _app()
    _wire_mock_upstream(
        app, lambda r: httpx.Response(302, headers={"location": "https://example.org/x"})
    )
    r = TestClient(app).get("/provider/work-items", follow_redirects=False)
    assert r.headers["location"] == "https://example.org/x"


# --- verbatim forwarding ---------------------------------------------------


def _capture(app):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["raw"] = request.url.raw_path
        return httpx.Response(200, json={})

    _wire_mock_upstream(app, handler)
    return seen


def test_repeated_query_keys_all_reach_the_plugin():
    app = _app()
    seen = _capture(app)
    assert TestClient(app).get("/provider/work-items?state=open&state=blocked&x=1").status_code == 200
    assert seen["raw"] == b"/provider/work-items?state=open&state=blocked&x=1"


def test_percent_encoded_path_segments_are_forwarded_verbatim():
    app = _app()
    seen = _capture(app)
    assert TestClient(app).get("/provider/items/a%2Fb%23c%3Fd%2541").status_code == 200
    # Not re-segmented, no fragment/query peeled off, no double-decode.
    assert seen["raw"] == b"/provider/items/a%2Fb%23c%3Fd%2541"


def test_trailing_slash_is_forwarded_not_stripped():
    app = _app()
    seen = _capture(app)
    assert TestClient(app).get("/provider/work-items/").status_code == 200
    assert seen["raw"] == b"/provider/work-items/"


def test_plugin_base_url_sub_path_is_kept():
    reg = PluginRegistry()
    reg.upsert(
        PluginManifest(
            name="pm", base_url="http://pm-host:8802/pm",
            http=[{"prefix": "/provider"}],
        )
    )
    app = _app(reg)
    seen = _capture(app)
    assert TestClient(app).get("/provider/x").status_code == 200
    assert seen["raw"] == b"/pm/provider/x"


@pytest.mark.parametrize(
    "path,ok",
    [
        ("/provider/x", True),
        ("/provider/x/", True),        # trailing slash: a distinct, legitimate path
        ("/provider/./x", False),
        ("/provider/../provider/x", False),
        ("/provider//x", False),       # empty INTERIOR segment
    ],
)
def test_well_formed_refuses_dot_and_empty_interior_segments_only(path, ok):
    # Nothing is normalized: a malformed path is simply not the proxy's (404),
    # so what matched a prefix is always byte-identical to what is forwarded.
    assert http_proxy._well_formed(path) is ok


def test_unforwardable_raw_path_raises_proxy_path_error():
    with pytest.raises(http_proxy.ProxyPathError):
        http_proxy._raw_route_path(
            {"type": "http", "path": "/provider/x",
             "raw_path": "/provider/\xff".encode("latin-1"), "root_path": ""}
        )


def test_upstream_response_streams_chunks_intact():
    app = _app()

    async def chunks():
        yield b'{"items": ['
        yield b'{"id": 1}'
        yield b"]}"

    _wire_mock_upstream(
        app,
        lambda r: httpx.Response(
            200, content=chunks(), headers={"content-type": "application/json"}
        ),
    )
    r = TestClient(app).get("/provider/work-items")
    assert r.status_code == 200
    assert r.json() == {"items": [{"id": 1}]}


def test_405_carries_allow():
    app = _app(_registry(methods=["GET"]))
    r = TestClient(app).post("/provider/work-items")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET"


def test_config_named_surface_segment_is_refused_to_plugins(monkeypatch):
    """A surface that exists only in config (`SNOWLINE_SURFACES=…,ops` mounts
    `/ops/mcp`) is not in the STATIC reserved set; the registry refuses it
    from the LIVE route walk instead — no half-dead surface."""
    from snowline_platform.registry import HttpPrefixConflict

    monkeypatch.setenv("SNOWLINE_SURFACES", "main,shadow,ops")
    reg = PluginRegistry()
    _app(reg)
    assert "ops" in reg.reserved_http_prefixes
    with pytest.raises(HttpPrefixConflict) as exc:
        reg.upsert(PluginManifest(name="x", base_url="http://x:1", http=[{"prefix": "/ops"}]))
    assert exc.value.holder == "platform"


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

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.raw_path)
        return httpx.Response(200, json={})

    _wire_mock_upstream(app, handler)
    client = TestClient(app)
    # Percent-encoded dot-segments survive the client and reach the route —
    # and are REFUSED, never normalized: a path that could match one prefix
    # after normalization and be forwarded as another is not forwarded at all.
    assert client.get("/provider/x/%2e%2e/work-items").status_code == 404
    # Climbing OUT of the declared prefix — including onto a platform path —
    # is a plain 404, never a proxied or re-routed request.
    assert client.get("/provider/%2e%2e/plugins").json() == {"detail": "Not Found"}
    assert client.get("/provider/%2e%2e/nope").status_code == 404
    assert calls == []


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


# --- replication via the gateway: /via/<plugin>/… (decision 0b8390f7) -------


def _replicating_registry(status: PluginStatus | None = None) -> PluginRegistry:
    reg = PluginRegistry()
    reg.upsert(
        PluginManifest(
            name="pm",
            base_url="http://pm-host:8802",
            replication={
                "contract_version": 2,
                "ingest_path": "/events/ingest",
                "events": ["pm.item.created"],
            },
        )
    )
    # A registered plugin that does NOT replicate — nothing of it is reachable
    # through /via.
    reg.upsert(PluginManifest(name="walkthrough", base_url="http://wt-host:3417"))
    if status is not None:
        reg.set_status("pm", status)
    return reg


def test_via_forwards_ingest_post_verbatim_to_the_plugins_ingest_path():
    """The load-bearing property: `POST /via/pm/events/ingest` reaches pm as
    `POST <base_url>/events/ingest` with the body bytes and signature header
    untouched — the SDK's HMAC is over the exact body, so any rewrite would
    dead-letter every delivery."""
    app = _app(_replicating_registry())
    body = b'[{"seq": 1, "type": "pm.item.created", "payload": {"x": "\xc3\xa9"}}]'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://pm-host:8802/events/ingest?epoch=abc")
        assert request.content == body
        assert request.headers["x-snowline-signature"] == "sha256=deadbeef"
        assert request.headers["x-snowline-gateway"] == "1"
        return httpx.Response(202, json={"accepted": 1})

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(
        "/via/pm/events/ingest?epoch=abc",
        content=body,
        headers={"content-type": "application/json", "x-snowline-signature": "sha256=deadbeef"},
    )
    assert r.status_code == 202
    assert r.json() == {"accepted": 1}


def test_via_serves_the_replication_admin_surface():
    """The §5 handshake and the §7 snapshot live under `/replication-admin`
    on the plugin; a peer drives them through the gateway."""
    app = _app(_replicating_registry())
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.url.path.endswith("/snapshot"):
            return httpx.Response(200, content=b"PGDMP\x00\x01", headers={"content-type": "application/octet-stream"})
        return httpx.Response(200, json=[])

    _wire_mock_upstream(app, handler)
    c = TestClient(app)
    assert c.get("/via/pm/replication-admin/outbound").json() == []
    snap = c.post("/via/pm/replication-admin/snapshot", content=b'{"source_id":"x"}')
    assert snap.status_code == 200 and snap.content == b"PGDMP\x00\x01"
    assert snap.headers["content-type"] == "application/octet-stream"
    assert seen == [
        ("GET", "http://pm-host:8802/replication-admin/outbound"),
        ("POST", "http://pm-host:8802/replication-admin/snapshot"),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/via/pm/mcp",                    # the plugin's MCP surface
        "/via/pm/health",                 # its health route
        "/via/pm/ui-api/widgets/x",       # its dashboard data plane
        "/via/pm/provider/work-items",    # an `http` contract surface
        "/via/pm/events",                 # a prefix of the ingest path
        "/via/pm/events/ingest/extra",    # a path UNDER the ingest path
        "/via/pm/replication-adminx",     # a lookalike of the admin prefix
        "/via/pm",                        # no path at all
        "/via/pm/",
        "/via/walkthrough/events/ingest", # a plugin with no replication block
        "/via/nope/events/ingest",        # an unknown plugin
        "/via/pm/../governance/events/ingest",
        "/via/pm//events/ingest",
        "/via",
        "/via/",
    ],
)
def test_via_exposes_only_the_two_replication_surfaces(path):
    """Everything that is not the named plugin's declared ingest_path or its
    `/replication-admin/…` surface is a plain 404 — the app's own shape — with
    NO upstream round-trip. `/via` widens a plugin's tailnet exposure to
    exactly its replication surfaces and nothing else."""
    app = _app(_replicating_registry())
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(path, content=b"{}")
    assert r.status_code == 404, path
    assert r.json() == {"detail": "Not Found"}
    assert calls == []


def test_via_down_plugin_is_503_without_a_round_trip():
    app = _app(_replicating_registry(status=PluginStatus.DOWN))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200)

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post("/via/pm/events/ingest", content=b"[]")
    assert r.status_code == 503
    assert calls == []


def test_via_unreachable_plugin_is_502():
    app = _app(_replicating_registry())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post("/via/pm/events/ingest", content=b"[]")
    assert r.status_code == 502


def test_via_body_cap_is_sized_for_delivery_batches():
    """An ingest batch is far larger than a contract write: a body over the
    contract proxy's 64 KiB cap is forwarded under /via, and one over the
    via cap is 413 without a round-trip."""
    app = _app(_replicating_registry())
    sizes = []

    def handler(request: httpx.Request) -> httpx.Response:
        sizes.append(len(request.content))
        return httpx.Response(202)

    _wire_mock_upstream(app, handler)
    c = TestClient(app)
    big = b"x" * (POST_BODY_LIMIT * 4)
    assert c.post("/via/pm/events/ingest", content=big).status_code == 202
    assert sizes == [len(big)]
    too_big = b"x" * (http_proxy.VIA_BODY_LIMIT + 1)
    assert c.post("/via/pm/events/ingest", content=too_big).status_code == 413
    assert sizes == [len(big)]


def test_via_prefix_is_reserved_to_plugins():
    """`via` is the platform's — a plugin cannot claim it as an `http` prefix
    (the static reserved set refuses at validation) and the built app's live
    route walk lists it too, so the two lines of defense agree."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="reserved"):
        PluginManifest(name="x", base_url="http://x:1", http=[{"prefix": "/via"}])
    reg = PluginRegistry()
    _app(reg)
    assert "via" in reg.reserved_http_prefixes


def test_via_is_trust_gated_like_every_gateway_path():
    """The mount rides TrustMiddleware: an untrusted peer gets the gate's 403
    before the proxy (and the plugin) is ever reached."""
    from snowline_platform.trust import TrustResolver as _TR

    class _NeverTrust:
        def resolve(self, peer_ip, headers):
            return None

    app = _app(_replicating_registry(), resolver=_TR([_NeverTrust()]))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(202)

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post("/via/pm/events/ingest", content=b"[]")
    assert r.status_code == 403
    assert calls == []
