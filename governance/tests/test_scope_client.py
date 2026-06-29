"""The HTTP scope carry — `HttpScopeClient` against the platform's real
response shapes (stubbed with `httpx.MockTransport`, no platform running).

This pins the contract governance depends on: `GET /scopes/{slug}/ancestors`
returns `{"ancestors": [<to_row>, ...]}` and `GET /scopes/{slug}` returns a
`to_row` (404 → None / ScopeNotFoundError). No DB needed.
"""

from __future__ import annotations

import httpx
import pytest

from snowline_governance.scope_client import (
    HttpScopeClient,
    ScopeNotFoundError,
    ScopeServiceError,
)


def _row(slug: str, isolated: bool = False) -> dict:
    return {
        "id": f"id-{slug}",
        "slug": slug,
        "name": slug,
        "kind": "project",
        "status": "active",
        "isolated": isolated,
        "org": slug.split("/", 1)[0],
    }


def _client(handler) -> HttpScopeClient:
    return HttpScopeClient(
        "http://platform.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_ancestors_parses_platform_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/scopes/acme/widget/ancestors"
        return httpx.Response(
            200, json={"ancestors": [_row("acme/widget"), _row("acme")]}
        )

    chain = _client(handler).ancestors("acme/widget")
    assert [s["slug"] for s in chain] == ["acme/widget", "acme"]


def test_ancestors_404_raises_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no scope"})

    with pytest.raises(ScopeNotFoundError):
        _client(handler).ancestors("ghost")


def test_resolve_returns_row_or_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/scopes/acme":
            return httpx.Response(200, json=_row("acme"))
        return httpx.Response(404, json={"detail": "no scope"})

    c = _client(handler)
    assert c.resolve("acme")["slug"] == "acme"
    assert c.resolve("ghost") is None


def test_unreachable_platform_raises_service_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(ScopeServiceError):
        _client(handler).ancestors("acme")
