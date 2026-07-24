"""The milestone dependency, over HTTP (milestones.md §6.1).

Governance does NOT own milestones — the platform does. But since #145 the
`ArtifactVersion.milestone` stamp is no longer a soft verbatim slug: it is a
**resolution key**, and version canonicality is a function of milestone STATE
read from the platform (milestones.md §6.1). So governance resolves + reads
milestone status over HTTP, exactly like the scope surface:

  GET  /milestones/resolve?ref=&context=   single-ref resolution (§3) — the
                                           write path validates a stamp at mint.
  POST /milestones/resolve-batch           many refs → per-ref status in ONE
                                           round-trip — the read path's
                                           canonicality computation (§6.1.2).
  GET  /milestones/{address}/aliases       the tombstone closure for a target —
                                           milestone-keyed reads match stored
                                           stamps against the FULL alias set (§5).

`MilestoneClient` is a thin Protocol so tests can STUB it (no running platform
in unit tests). `HttpMilestoneClient` is the real httpx implementation. The
artifact canonicality logic depends on the protocol, never on httpx directly,
so it is fully testable with an in-memory fake.

Two error shapes, mirroring the scope client's transport-vs-lookup split:

  - `MilestoneServiceError` (transport / 5xx) — the service was unreachable or
    returned a server error. This is a HARD error on a governance read (§6.1.2):
    a milestone-status read failure must NEVER be treated as an absent stamp,
    because a transient platform outage must not silently flip canonicality.

  - `MilestoneResolutionError` (a 404 miss) — the ref does not resolve. Carries
    the platform's `suggestions` (near-miss / same-named candidates), NEVER an
    automatic resolution (bare names never resolve outside the walk; unknown
    never mints). The write path surfaces these to the agent on a hard-fail.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from snowline_governance import config


class MilestoneServiceError(RuntimeError):
    """The platform milestone service was unreachable or returned a server error
    (transport failure / 5xx). A HARD error on the governance read — an
    unreadable stamp is NEVER treated as absent (§6.1.2)."""


class MilestoneResolutionError(LookupError):
    """A ref could not be resolved (§3). Carries `suggestions` — the platform's
    near-miss or same-named candidates, surfaced for the agent, NEVER an
    automatic resolution."""

    def __init__(self, message: str, suggestions: list[dict] | None = None) -> None:
        super().__init__(message)
        self.suggestions = suggestions or []


@runtime_checkable
class MilestoneClient(Protocol):
    """The read/resolve surface governance needs from the platform's milestone
    service (milestones.md §5).

    Three reads:
      - `resolve(ref, context=None)` — a single ref → the platform milestone row
        (`{address, status, resolved_via_alias, ...}`). The write path uses this
        to validate + canonicalize a stamp at mint. Raises
        `MilestoneResolutionError` (carrying suggestions) on an unknown ref.
      - `resolve_batch(refs, context=None)` — many refs → `{ref: {address,
        status, resolved_via_alias}}` on success or `{ref: {error, suggestions}}`
        on a miss, in ONE round-trip. The read path's canonicality computation
        (a single unresolvable stamp buckets as legacy, never failing the batch).
      - `aliases(address)` — `{target, aliases: [...]}`, the tombstone closure so
        milestone-keyed reads match stored stamps against the full alias set.
    Any of them raises `MilestoneServiceError` if the platform is
    unreachable/erroring (a hard error the read propagates, never swallows).
    """

    def resolve(self, ref: str, context: str | None = None) -> dict: ...

    def resolve_batch(
        self, refs: list[str], context: str | None = None
    ) -> dict[str, dict]: ...

    def aliases(self, address: str) -> dict: ...


class HttpMilestoneClient:
    """Real `MilestoneClient` — calls the platform's milestone read/resolve API
    over httpx.

    `platform_url` defaults to `config.platform_url()` (SNOWLINE_PLATFORM_URL) —
    the SAME platform the scope client talks to. A caller may inject a pre-built
    `httpx.Client` (to share a connection pool); otherwise one is created per
    call. Behind the platform trust gate the request rides the tailnet — no
    per-request secret (the SSH-into-host daily flow stays transparent), exactly
    as `HttpScopeClient`.
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

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        url = f"{self._platform_url}{path}"
        try:
            if self._client is not None:
                return self._client.get(url, params=params, timeout=self._timeout)
            return httpx.get(url, params=params, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise MilestoneServiceError(
                f"platform milestone service unreachable at {url!r}: {exc}"
            ) from exc

    def _post(self, path: str, json: dict) -> httpx.Response:
        url = f"{self._platform_url}{path}"
        try:
            if self._client is not None:
                return self._client.post(url, json=json, timeout=self._timeout)
            return httpx.post(url, json=json, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise MilestoneServiceError(
                f"platform milestone service unreachable at {url!r}: {exc}"
            ) from exc

    @staticmethod
    def _resolution_error(resp: httpx.Response, ref: str) -> MilestoneResolutionError:
        """Build a `MilestoneResolutionError` from a 404 body. FastAPI wraps the
        route's `HTTPException(404, {"detail": ..., "suggestions": ...})` as
        `{"detail": {"detail": ..., "suggestions": ...}}`, so the near-miss
        candidates live one level in."""
        try:
            body = resp.json()
        except ValueError:
            body = {}
        detail = body.get("detail", {}) if isinstance(body, dict) else {}
        if isinstance(detail, dict):
            msg = detail.get("detail") or f"unknown milestone {ref!r}"
            suggestions = detail.get("suggestions", []) or []
        else:
            msg, suggestions = str(detail), []
        return MilestoneResolutionError(msg, suggestions)

    def resolve(self, ref: str, context: str | None = None) -> dict:
        params = {"ref": ref}
        if context is not None:
            params["context"] = context
        resp = self._get("/milestones/resolve", params=params)
        if resp.status_code == 404:
            raise self._resolution_error(resp, ref)
        if resp.status_code >= 400:
            raise MilestoneServiceError(
                f"platform milestone service returned {resp.status_code} "
                f"resolving {ref!r}"
            )
        return resp.json()

    def resolve_batch(
        self, refs: list[str], context: str | None = None
    ) -> dict[str, dict]:
        body: dict = {"refs": list(refs)}
        if context is not None:
            body["context"] = context
        resp = self._post("/milestones/resolve-batch", json=body)
        if resp.status_code >= 400:
            # The batch endpoint returns 200 with per-ref {error} for misses; a
            # >=400 here is a real service/transport error — a HARD read error
            # (§6.1.2), never a silent "all stamps absent".
            raise MilestoneServiceError(
                f"platform milestone service returned {resp.status_code} for a "
                f"resolve-batch of {len(refs)} ref(s)"
            )
        return resp.json().get("results", {})

    def aliases(self, address: str) -> dict:
        resp = self._get(f"/milestones/{address}/aliases")
        if resp.status_code == 404:
            raise self._resolution_error(resp, address)
        if resp.status_code >= 400:
            raise MilestoneServiceError(
                f"platform milestone service returned {resp.status_code} for "
                f"aliases of {address!r}"
            )
        return resp.json()
