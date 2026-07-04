"""The tailnet-gated replication HTTP surface — the ingest route + the
replication-admin routes (replication-continuity §5, issue #77).

Pairing can no longer be purely programmatic: subscriptions are rows in each
plugin's OWN store, and a platform-level CLI cannot reach into governance's,
memory's, and pm's databases. So the SDK ships this small HTTP surface next to
`ingest_path`, and `snowline replicate pair` (§9 item 6, #82) drives both sides
over it — create/list/retire inbound stream registrations and outbound
subscriptions, the receiver-mints-secret handshake, rotation. This SUPERSEDES
the bus's "no remote surface in v1" posture for REPLICATION-CLASS subscriptions
only (the posture's two records — the SDK `events.py` docstring and governance
`replication.py`'s subscription-management note — carry pointers here), and it
stays OFF MCP: agents never manage plumbing.

Trust: every route (ingest included — "the trust gate applies unchanged", §5)
is gated on the peer IP against `SNOWLINE_TRUSTED_CIDRS`, defaulting to the
tailnet + loopback set §5.1 prescribes. The spec's config trap applies: the env
var REPLACES the default when set — state the full list, and remember that
behind a `tailscale serve` → loopback front every request arrives with a
LOOPBACK peer IP, so the loopback entries are what admit cross-instance
traffic. The HMAC secret authenticates the *stream*; this gate authenticates
the *network* — no new auth surface.

This module pulls `fastapi` and is deliberately NOT re-exported from the
`replication` package root — import it explicitly.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Callable
from contextlib import AbstractContextManager

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from snowline_plugin_sdk.replication import emit as _emit
from snowline_plugin_sdk.replication import ingest as _ingest


# §5.1's full trusted set: the tailnet CGNAT range + IPv4/IPv6 loopback.
# SNOWLINE_TRUSTED_CIDRS REPLACES this when set (the documented config trap).
DEFAULT_TRUSTED_CIDRS = "100.64.0.0/10,127.0.0.0/8,::1"


def trusted_networks() -> list:
    """The trusted CIDR set, read live from `SNOWLINE_TRUSTED_CIDRS` (default:
    tailnet + loopback, §5.1)."""
    raw = os.environ.get("SNOWLINE_TRUSTED_CIDRS", DEFAULT_TRUSTED_CIDRS)
    return [
        ipaddress.ip_network(c.strip(), strict=False)
        for c in raw.split(",")
        if c.strip()
    ]


def _require_trusted(request: Request) -> None:
    """Reject any request whose peer IP is outside the trusted set. Mirrors the
    platform's `CidrTrustProvider` posture: a network gate, not identity —
    sufficient inside the tailnet boundary; the stream HMAC does the rest.

    `request.client.host` is the SOCKET peer; forwarded-for headers are
    deliberately not consulted. DEPLOYMENT TRAP: running the plugin with
    proxy-header trust enabled (uvicorn `--proxy-headers` /
    `ProxyHeadersMiddleware`) rewrites `request.client` FROM
    `X-Forwarded-For` — behind any front that isn't itself the §5.1
    tailscale-serve/loopback path, that lets an untrusted client spoof a
    trusted peer IP with one header. Never enable proxy-header trust on an
    app serving these routes unless the only reachable front is the trusted
    proxy itself."""
    peer = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        raise HTTPException(status_code=403, detail="untrusted peer") from None
    if not any(ip in net for net in trusted_networks()):
        raise HTTPException(status_code=403, detail="untrusted peer")


def _required(data: dict, *fields: str) -> list:
    # Presence, not truthiness: a legitimate falsy value (an empty
    # event_types list) must not read as missing.
    missing = [f for f in fields if f not in data or data[f] is None]
    if missing:
        raise HTTPException(
            status_code=400, detail=f"missing required field(s): {', '.join(missing)}"
        )
    return [data[f] for f in fields]


def build_replication_router(
    session_scope: Callable[[], AbstractContextManager[Session]],
    apply,
    *,
    ingest_path: str = "/events/ingest",
    admin_prefix: str = "/replication-admin",
) -> APIRouter:
    """The plugin's replication HTTP surface: POST `ingest_path` (the manifest's
    declared ingest route, §4) plus the §5 admin routes under `admin_prefix`.
    `session_scope` is the plugin's own transactional session context;
    `apply` is its idempotent domain apply (see `ingest.ingest_delivery`).
    Include the returned router in the plugin's FastAPI app BEFORE any
    catch-all MCP mounts (the same ordering note as `/health`)."""
    router = APIRouter()

    @router.post(ingest_path)
    async def ingest(request: Request) -> JSONResponse:
        _require_trusted(request)
        body = await request.body()
        signature = request.headers.get("X-Snowline-Signature")
        with session_scope() as session:
            status, payload = _ingest.ingest_delivery(session, body, signature, apply)
        return JSONResponse(payload, status_code=status)

    # --- inbound registrations (receiver side of the §5 handshake) ----------

    @router.post(f"{admin_prefix}/inbound")
    async def register_inbound(request: Request, data: dict) -> dict:
        _require_trusted(request)
        source_id, epoch = _required(data, "source_id", "epoch")
        try:
            with session_scope() as session:
                # The minted secret rides this one response over the tailnet
                # (WireGuard-encrypted transport) and is never listed or
                # logged again (§5).
                registered = _ingest.register_inbound_stream(
                    session, source_id, epoch
                )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except IntegrityError:
            # Two concurrent registrations raced past the existence check; the
            # PK collision surfaces at the scope-exit commit — same verdict as
            # the checked path, not a 500.
            raise HTTPException(
                status_code=409,
                detail=f"inbound stream ({source_id!r}, {epoch!r}) already exists",
            ) from None
        return registered

    @router.get(f"{admin_prefix}/inbound")
    async def list_inbound(request: Request) -> list[dict]:
        _require_trusted(request)
        with session_scope() as session:
            return _ingest.list_inbound_streams(session)

    @router.post(f"{admin_prefix}/inbound/rotate")
    async def rotate_inbound(request: Request, data: dict) -> dict:
        _require_trusted(request)
        source_id, epoch = _required(data, "source_id", "epoch")
        with session_scope() as session:
            try:
                return _ingest.rotate_inbound_secret(session, source_id, epoch)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post(f"{admin_prefix}/inbound/retire")
    async def retire_inbound(request: Request, data: dict) -> dict:
        _require_trusted(request)
        source_id, epoch = _required(data, "source_id", "epoch")
        with session_scope() as session:
            try:
                return _ingest.retire_inbound_stream(session, source_id, epoch)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None

    # --- outbound subscriptions (sender side) --------------------------------

    @router.post(f"{admin_prefix}/outbound")
    async def create_outbound(request: Request, data: dict) -> dict:
        _require_trusted(request)
        target_url, secret, event_types, epoch = _required(
            data, "target_url", "secret", "event_types", "epoch"
        )
        if not isinstance(event_types, list):
            # A bare string would list()-explode into characters downstream.
            raise HTTPException(
                status_code=400, detail="event_types must be a list of event names"
            )
        with session_scope() as session:
            return _emit.create_outbound_subscription(
                session,
                target_url,
                secret,
                list(event_types),
                epoch=epoch,
                source_id=data.get("source_id"),
                peer_source_id=data.get("peer_source_id"),
            )

    @router.get(f"{admin_prefix}/outbound")
    async def list_outbound(request: Request) -> list[dict]:
        _require_trusted(request)
        with session_scope() as session:
            return _emit.list_outbound_subscriptions(session)

    @router.post(f"{admin_prefix}/outbound/retire")
    async def retire_outbound(request: Request, data: dict) -> dict:
        _require_trusted(request)
        (subscription_id,) = _required(data, "id")
        with session_scope() as session:
            try:
                return _emit.retire_outbound_subscription(session, subscription_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post(f"{admin_prefix}/outbound/secret")
    async def update_outbound_secret(request: Request, data: dict) -> dict:
        _require_trusted(request)
        subscription_id, secret = _required(data, "id", "secret")
        with session_scope() as session:
            try:
                return _emit.set_subscription_secret(session, subscription_id, secret)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None

    # --- parking: the loud read (§8.1) ---------------------------------------

    @router.get(f"{admin_prefix}/parked")
    async def parked(request: Request) -> list[dict]:
        _require_trusted(request)
        with session_scope() as session:
            return _ingest.list_parked(session)

    # --- dead-letters: the sender-side mirror (§3.1) --------------------------

    @router.get(f"{admin_prefix}/rejected")
    async def rejected(request: Request) -> list[dict]:
        _require_trusted(request)
        with session_scope() as session:
            return _emit.list_rejected(session)

    @router.post(f"{admin_prefix}/rejected/requeue")
    async def requeue_rejected(request: Request, data: dict) -> dict:
        _require_trusted(request)
        (row_id,) = _required(data, "id")
        with session_scope() as session:
            try:
                return _emit.requeue_rejected(session, row_id)
            except _emit.RequeueRefusedError as exc:
                # More specific than the plain ValueError below (it IS one) —
                # caught first so a retired-subscription refusal (§108) answers
                # 409 with the successor pointer, not a bare 404.
                raise HTTPException(status_code=409, detail=exc.detail) from None
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post(f"{admin_prefix}/rejected/requeue-bulk")
    async def requeue_rejected_bulk(request: Request, data: dict) -> dict:
        _require_trusted(request)
        (subscription_id,) = _required(data, "subscription_id")
        with session_scope() as session:
            try:
                return _emit.requeue_rejected_bulk(
                    session,
                    subscription_id,
                    event_type=data.get("event_type"),
                    reason=data.get("reason"),
                )
            except _emit.RequeueRefusedError as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from None
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None

    return router
