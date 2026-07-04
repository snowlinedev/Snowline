"""The /ui-api proxy (ui-shell.md §5).

The seam: `ui_api._client` lazily creates + caches an `httpx.AsyncClient` on
`app.state.ui_api_client` on first use. Pre-seeding that attribute with an
`httpx.AsyncClient(transport=httpx.MockTransport(...))` — the same technique
`test_health.py` uses for the health poller's client — is the cleanest way to
stand in for a live plugin's `/ui-api` surface without a socket: no need for
the gateway's MCP-specific `UpstreamConnector` abstraction, since this proxy
speaks plain HTTP/JSON, not MCP.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

from snowline_platform import ui_api
from snowline_platform.app import create_app
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry, PluginStatus
from snowline_platform.trust import Principal, TrustResolver


class _AlwaysTrust:
    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _app(registry: PluginRegistry) -> object:
    return create_app(
        resolver=TrustResolver([_AlwaysTrust()]),
        registry=registry,
        migrate_on_startup=False,
    )


def _registry(status: PluginStatus | None = None) -> PluginRegistry:
    reg = PluginRegistry()
    reg.upsert(PluginManifest(name="gov", base_url="http://plugin-host:9999"))
    if status is not None:
        reg.set_status("gov", status)
    return reg


def _registry_with_composer(status: PluginStatus | None = None) -> PluginRegistry:
    """A plugin that declared a `thread` page with a `composer` — the only
    POST write target the proxy's structural allowlist admits (shadow-
    conversations.md §3/§4)."""
    reg = PluginRegistry()
    reg.upsert(
        PluginManifest(
            name="gov",
            base_url="http://plugin-host:9999",
            ui={
                "pages": [
                    {
                        "id": "shadow-branch",
                        "route": "/shadow/{branch}",
                        "kind": "thread",
                        "data": "/ui-api/pages/branches/{branch}",
                        "composer": {
                            "endpoint": "/ui-api/pages/branches/{branch}/messages",
                            "placeholder": "Reply in this branch…",
                        },
                    }
                ]
            },
        )
    )
    if status is not None:
        reg.set_status("gov", status)
    return reg


def _wire_mock_upstream(app, handler) -> None:
    app.state.ui_api_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )


def test_proxy_happy_path_forwards_status_body_query_and_content_type():
    app = _app(_registry())

    def handler(request: httpx.Request) -> httpx.Response:
        # The mapping rule: GET /ui-api/<plugin>/<path> -> GET
        # <base_url>/ui-api/<path> — the plugin name is consumed by lookup,
        # never forwarded into the upstream path.
        assert request.url.host == "plugin-host"
        assert request.url.path == "/ui-api/pages/branches"
        assert request.url.params["x"] == "1"
        return httpx.Response(200, json={"rows": []})

    _wire_mock_upstream(app, handler)
    r = TestClient(app).get("/ui-api/gov/pages/branches?x=1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"rows": []}


def test_proxy_unregistered_plugin_is_404():
    app = _app(PluginRegistry())
    r = TestClient(app).get("/ui-api/ghost/pages/branches")
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_proxy_down_plugin_is_503_without_a_network_call():
    app = _app(_registry(status=PluginStatus.DOWN))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the upstream for a DOWN plugin")

    _wire_mock_upstream(app, handler)
    r = TestClient(app).get("/ui-api/gov/pages/branches")
    assert r.status_code == 503


def test_proxy_unknown_status_and_up_status_proceed():
    # UNKNOWN (fresh registration, not yet health-checked) and UP both route
    # through — only an explicit DOWN short-circuits (mirrors the gateway's
    # discover_upstreams routability rule).
    for status in (None, PluginStatus.UP):
        app = _app(_registry(status=status))
        _wire_mock_upstream(app, lambda r: httpx.Response(200, json={}))
        assert TestClient(app).get("/ui-api/gov/pages/branches").status_code == 200


@pytest.mark.parametrize("verb", ["put", "delete", "patch"])
def test_proxy_other_verbs_are_405(verb):
    # Only GET and POST are wired (ui-shell.md §5); every other verb keeps
    # the normal Starlette method-not-allowed behavior — pinned per verb so a
    # future router change can't quietly widen the write seam.
    app = _app(_registry())
    r = getattr(TestClient(app), verb)("/ui-api/gov/pages/branches")
    assert r.status_code == 405


def test_proxy_upstream_connect_error_is_502():
    app = _app(_registry())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _wire_mock_upstream(app, handler)
    r = TestClient(app).get("/ui-api/gov/pages/branches")
    assert r.status_code == 502
    assert "gov" in r.json()["detail"]


def test_proxy_traversal_cannot_escape_ui_api_prefix():
    app = _app(_registry())

    def handler(request: httpx.Request) -> httpx.Response:
        # Whatever the client sent, the upstream request always lands under
        # /ui-api/ — never at the plugin's /mcp or any other route.
        assert request.url.path == "/ui-api/mcp"
        return httpx.Response(200, json={"ok": True})

    _wire_mock_upstream(app, handler)
    # A literal '..' is normalized away by the CLIENT itself before the
    # request is even sent (httpx's own URL handling collapses dot-segments
    # client-side) — the percent-encoded form survives to reach the route,
    # same reasoning as the /ui SPA's own traversal test
    # (test_surfaces_routes.py's "%2e%2e" case).
    r = TestClient(app).get("/ui-api/gov/%2e%2e/%2e%2e/mcp")
    assert r.status_code == 200


def test_proxy_shares_one_client_across_requests():
    app = _app(_registry())
    _wire_mock_upstream(app, lambda r: httpx.Response(200, json={}))
    client = TestClient(app)
    seeded = app.state.ui_api_client
    assert client.get("/ui-api/gov/a").status_code == 200
    assert client.get("/ui-api/gov/b").status_code == 200
    # No per-request client was created — the seeded one is still in use.
    assert app.state.ui_api_client is seeded


# --- POST: the write seam (shadow-conversations.md §3) ----------------------


def test_proxy_post_reaches_declared_composer_endpoint_and_round_trips_body():
    app = _app(_registry_with_composer())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "plugin-host"
        assert request.url.path == "/ui-api/pages/branches/abc/messages"
        assert request.headers["content-type"] == "application/json"
        assert request.content == b'{"markdown": "hi"}'
        return httpx.Response(201, json={"seq": 1, "markdown": "hi"})

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/abc/messages",
        content=b'{"markdown": "hi"}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 201
    assert r.json() == {"seq": 1, "markdown": "hi"}


def test_proxy_post_undeclared_path_is_403():
    app = _app(_registry_with_composer())

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the upstream for an undeclared path")

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/abc/some-other-route",
        json={"markdown": "hi"},
    )
    assert r.status_code == 403


def test_proxy_post_with_no_composer_declared_is_403():
    # A plugin with no `ui` block at all has no declared write endpoints —
    # every POST 403s.
    app = _app(_registry())

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the upstream")

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post("/ui-api/gov/pages/branches", json={"x": 1})
    assert r.status_code == 403


def test_proxy_post_unregistered_plugin_is_404():
    app = _app(PluginRegistry())
    r = TestClient(app).post("/ui-api/ghost/anything", json={})
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_proxy_post_down_plugin_is_503_without_a_network_call():
    app = _app(_registry_with_composer(status=PluginStatus.DOWN))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the upstream for a DOWN plugin")

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/abc/messages", json={"markdown": "hi"}
    )
    assert r.status_code == 503


def test_proxy_post_non_json_content_type_is_415():
    app = _app(_registry_with_composer())

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the upstream for a bad content-type")

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/abc/messages",
        content=b"markdown=hi",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 415


def test_proxy_post_json_with_charset_content_type_is_accepted():
    app = _app(_registry_with_composer())
    _wire_mock_upstream(app, lambda r: httpx.Response(200, json={"ok": True}))
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/abc/messages",
        content=b'{"markdown": "hi"}',
        headers={"content-type": "application/json; charset=utf-8"},
    )
    assert r.status_code == 200


def test_proxy_post_oversize_body_by_content_length_is_413():
    # The Content-Length precheck rejects before ever reading the stream.
    app = _app(_registry_with_composer())

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the upstream for an oversize body")

    _wire_mock_upstream(app, handler)
    oversize = b"a" * (ui_api.POST_BODY_LIMIT + 1)
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/abc/messages",
        content=oversize,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413


def test_proxy_post_at_body_limit_is_accepted():
    # Exactly at the cap is fine — only strictly OVER it 413s.
    app = _app(_registry_with_composer())
    _wire_mock_upstream(app, lambda r: httpx.Response(200, json={"ok": True}))
    prefix, suffix = b'{"m": "', b'"}'
    at_limit = prefix + b"a" * (ui_api.POST_BODY_LIMIT - len(prefix) - len(suffix)) + suffix
    assert len(at_limit) == ui_api.POST_BODY_LIMIT
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/abc/messages",
        content=at_limit,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200


def test_proxy_post_oversize_body_with_lying_content_length_is_413():
    # A Content-Length that UNDERSTATES the real body must not slip through —
    # the streamed read enforces the cap regardless of the header.
    app = _app(_registry_with_composer())

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the upstream for an oversize body")

    _wire_mock_upstream(app, handler)
    oversize = b"a" * (ui_api.POST_BODY_LIMIT + 1)
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/abc/messages",
        content=oversize,
        headers={
            "content-type": "application/json",
            "content-length": "1",
        },
    )
    assert r.status_code == 413


def test_proxy_post_template_param_matches_exactly_one_segment():
    app = _app(_registry_with_composer())
    _wire_mock_upstream(app, lambda r: httpx.Response(200, json={"ok": True}))
    # One concrete segment for {branch} — matches.
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/abc/messages",
        json={"markdown": "hi"},
    )
    assert r.status_code == 200


def test_proxy_post_template_param_is_not_greedy():
    app = _app(_registry_with_composer())

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an extra path segment must not match the template")

    _wire_mock_upstream(app, handler)
    # Two segments where the template's {branch} expects exactly one —
    # segment COUNT must match, so this must NOT match
    # /pages/branches/{branch}/messages and 403s.
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/a/b/messages",
        json={"markdown": "hi"},
    )
    assert r.status_code == 403


def test_proxy_post_dot_segment_cannot_bypass_the_allowlist():
    # A `{branch}` template slot matches exactly one segment, non-greedily —
    # but pre-normalization it would also happily "match" a literal '..'.
    # The match MUST run against the same normalized path that gets
    # forwarded, so a dot-segment can't satisfy the {branch} slot on paper
    # while resolving to a completely different, undeclared upstream path.
    app = _app(_registry_with_composer())

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "a dot-segment must never reach the upstream via a bypassed "
            "allowlist check"
        )

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/%2e%2e/messages",
        json={"markdown": "hi"},
    )
    assert r.status_code in (403, 404)


def test_proxy_post_forwards_the_query_string():
    # Same posture as GET: the query string rides along to the upstream (an
    # idempotency key or csrf token on a write must not be silently stripped).
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    app = _app(_registry_with_composer())
    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/abc/messages?idempotency=xyz",
        json={"markdown": "hi"},
    )
    assert r.status_code == 200
    assert seen["query"] == {"idempotency": "xyz"}


def test_proxy_post_upstream_connect_error_is_502():
    app = _app(_registry_with_composer())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _wire_mock_upstream(app, handler)
    r = TestClient(app).post(
        "/ui-api/gov/pages/branches/abc/messages", json={"markdown": "hi"}
    )
    assert r.status_code == 502
