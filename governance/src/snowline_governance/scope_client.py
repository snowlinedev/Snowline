"""The scope dependency, over HTTP.

Governance does NOT own scopes — the platform does (architecture §2). But
`applicable_decisions` needs the ancestor chain (the isolation-halting walk) to
compute ancestor-inherited applicability, and that tree lives in the platform
now. So governance reads it over HTTP: `GET /scopes/{slug}/ancestors` returns the
reader scope first, then each `parent_id` ancestor nearest-first, HALTING at the
first `isolated` node and the forest root — the platform already does the walk
(scope-namespace spec §3), so governance just consumes the result.

`ScopeClient` is a thin protocol so tests can STUB it (no running platform needed
in unit tests). `HttpScopeClient` is the real httpx implementation. The
applicability logic depends on the protocol, never on httpx directly, so it's
fully testable with an in-memory fake.

A returned scope row is the platform's `to_row` shape:
`{slug, name, kind, status, isolated, org}`. Governance reads `slug` off each.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from snowline_governance import config


class ScopeNotFoundError(LookupError):
    """The platform has no scope with the given slug (a 404 from the read API)."""


class ScopeServiceError(RuntimeError):
    """The platform scope service was unreachable or returned an error status."""


@runtime_checkable
class ScopeClient(Protocol):
    """The read surface governance needs from the platform's scope service.

    Two reads this increment:
      - `resolve(slug)` — the platform's scope row (`GET /scopes/{slug}`), so a
        write can validate the scope exists + capture its `id` (the soft
        reference governance stores). `None` for an unknown slug.
      - `ancestors(slug)` — the isolation-halting applicability chain,
        nearest-first (the reader's own scope is element 0).
    Both return the platform's scope-row dicts. `ScopeServiceError` if the
    platform is unreachable/erroring.
    """

    def resolve(self, slug: str) -> dict | None: ...

    def ancestors(self, slug: str) -> list[dict]: ...


class HttpScopeClient:
    """Real `ScopeClient` — calls the platform's scope read API over httpx.

    `platform_url` defaults to `config.platform_url()`. A caller may inject a
    pre-built `httpx.Client` (e.g. to share a connection pool or set the trust
    headers); otherwise one is created per call. Behind the platform trust gate
    the request rides the tailnet — no per-request secret (the SSH-into-host
    daily flow stays transparent).
    """

    def __init__(
        self,
        platform_url: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._platform_url = (platform_url or config.platform_url()).rstrip("/")
        self._client = client
        self._timeout = timeout

    def _get(self, path: str) -> httpx.Response:
        url = f"{self._platform_url}{path}"
        try:
            if self._client is not None:
                return self._client.get(url, timeout=self._timeout)
            return httpx.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ScopeServiceError(
                f"platform scope service unreachable at {url!r}: {exc}"
            ) from exc

    def resolve(self, slug: str) -> dict | None:
        resp = self._get(f"/scopes/{slug}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise ScopeServiceError(
                f"platform scope service returned {resp.status_code} resolving "
                f"{slug!r}"
            )
        return resp.json()

    def ancestors(self, slug: str) -> list[dict]:
        resp = self._get(f"/scopes/{slug}/ancestors")
        if resp.status_code == 404:
            raise ScopeNotFoundError(f"no scope with slug {slug!r} (platform 404)")
        if resp.status_code >= 400:
            raise ScopeServiceError(
                f"platform scope service returned {resp.status_code} for "
                f"ancestors of {slug!r}"
            )
        return resp.json().get("ancestors", [])
