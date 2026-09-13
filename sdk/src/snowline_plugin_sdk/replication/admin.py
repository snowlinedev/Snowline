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

`POST {admin_prefix}/snapshot` (item 0ebe6a70 / #221) is the one route where
the trust gate is deliberately NOT sufficient on its own: it hands out the
service's whole database, and trusted-CIDR grants owner to every tailnet peer.
It additionally requires an HMAC signature under the secret of a LIVE OUTBOUND
subscription toward the caller — see `_authorize_snapshot`.

This module pulls `fastapi` and is deliberately NOT re-exported from the
`replication` package root — import it explicitly.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from snowline_plugin_sdk.replication import emit as _emit
from snowline_plugin_sdk.replication import ingest as _ingest
from snowline_plugin_sdk.replication import snapshot as _snapshot
from snowline_plugin_sdk.replication.envelope import REJECTION_REASONS, verify_signature
from snowline_plugin_sdk.replication.models import ReplicationSubscription


# §5.1's full trusted set: the tailnet CGNAT range + IPv4/IPv6 loopback.
# SNOWLINE_TRUSTED_CIDRS REPLACES this when set (the documented config trap).
# Both ranges are deliberate, platform-wide policy — not a tailnet-only
# default with loopback as an incidental widening (governance decision 35546152).
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


# The archive is streamed off disk in 1 MiB chunks — the service never holds a
# whole database dump in memory.
SNAPSHOT_CHUNK_BYTES = 1 << 20

# A process-lifetime decoy key. When no subscription matches the snapshot
# request we still burn one HMAC against this, so "no such stream" and "bad
# signature" cost the same and answer the same (see `_authorize_snapshot`).
_DECOY_SECRET = secrets.token_hex(32)


def _not_found() -> JSONResponse:
    """Byte-for-byte what an unmounted path answers, so an unauthenticated
    caller cannot distinguish a missing route from a refused one."""
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _authorize_snapshot(
    session: Session, body: bytes, signature: str | None
) -> ReplicationSubscription | None:
    """The snapshot route's authorization, and the reason it is safe to serve a
    whole database over the admin surface at all.

    The caller must present a body naming a stream —
    `{"source_id", "epoch", "peer_source_id"}` — AND an `X-Snowline-Signature`
    HMAC over the exact bytes of that body, under the secret of an ACTIVE
    OUTBOUND subscription on THIS service matching all three. Returns the
    subscription, or None (the route answers `_not_found()` either way — a
    caller cannot tell "no such stream" from "bad signature").

    This MECHANICALLY ENFORCES §7's prime-first/dump-second order on the
    primary side: the only secret that opens the snapshot is the one the seed
    minted while PRIMING the primary→spoke stream (§7 step 1), and priming is
    what makes the primary emit into that stream. **No prime, no snapshot** —
    an operator cannot take the dump before the stream exists and silently lose
    every write in the gap. It also scopes the exposure: a tailnet peer with no
    primed stream toward it gets nothing, which trusted-CIDR alone would not
    give (governance decision 1a83031c — the hub's Postgres never listens
    beyond loopback, so this surface is the ONLY path and it must not be
    peer-ambient).

    `active` is load-bearing for the `--reseed` path: a fresh-epoch re-seed
    retires the old subscription before priming the new one, so only the NEW
    epoch's secret opens the snapshot; a retired epoch's secret resolves
    nothing."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = {}
    source_id = data.get("source_id")
    epoch = data.get("epoch")
    peer_source_id = data.get("peer_source_id")
    sub = None
    if all(isinstance(v, str) for v in (source_id, epoch, peer_source_id)):
        sub = session.scalars(
            select(ReplicationSubscription).where(
                ReplicationSubscription.source_id == source_id,
                ReplicationSubscription.epoch == epoch,
                ReplicationSubscription.peer_source_id == peer_source_id,
                ReplicationSubscription.active.is_(True),
            )
        ).first()
    # Constant-time compare either way, against the decoy when nothing matched.
    ok = verify_signature(
        sub.secret if sub is not None else _DECOY_SECRET, body, signature
    )
    return sub if (ok and sub is not None) else None


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

    # --- snapshot: the §7 step-2 seed dump, served by its owner --------------

    @router.post(f"{admin_prefix}/snapshot")
    async def snapshot(request: Request) -> Response:
        """Stream a `pg_dump -Fc` custom-format archive of THIS service's OWN
        database — §7 step 2's seed snapshot, produced by the service that owns
        the store instead of by a remote `pg_dump` over a tailnet Postgres port
        (governance decision 1a83031c: the hub's Postgres stays loopback-only;
        all cross-instance data movement goes through a service's own HTTP
        surface behind the trust gate).

        Request: `POST {admin_prefix}/snapshot` with a JSON body
        `{"source_id", "epoch", "peer_source_id"}` naming the PRIMED
        primary→spoke stream, and `X-Snowline-Signature` = HMAC-SHA256 of the
        exact body bytes under that stream's secret. Response: `200` with
        `application/octet-stream` (the archive), or `404` with a
        nonexistent-route body for ANY authorization failure (see
        `_authorize_snapshot` — this is the route that mechanically enforces
        §7's prime-first/dump-second order: no prime, no snapshot).

        `_require_trusted` still runs FIRST, unchanged — the network gate is
        necessary, just not sufficient here."""
        _require_trusted(request)
        body = await request.body()
        signature = request.headers.get("X-Snowline-Signature")
        with session_scope() as session:
            if _authorize_snapshot(session, body, signature) is None:
                return _not_found()
            db_url = _snapshot.database_url(session)

        # A temp DIRECTORY, not NamedTemporaryFile: the archive must outlive
        # this coroutine (the streaming generator below owns its cleanup), and
        # pg_dump writes the file itself.
        tmpdir = tempfile.mkdtemp(prefix="snowline-snapshot-")
        dest = os.path.join(tmpdir, "snapshot.dump")
        try:
            # pg_dump is blocking and can run for a while — off the event loop.
            await run_in_threadpool(_snapshot.run_pg_dump, db_url, dest)
        except Exception as exc:  # noqa: BLE001 - reported to a verified peer
            shutil.rmtree(tmpdir, ignore_errors=True)
            # The caller holds this stream's secret, so the pg_dump error text
            # is safe to hand back — and a silent 500 would leave the seed
            # guessing. No password can appear in it (PGPASSWORD, never argv).
            raise HTTPException(status_code=500, detail=str(exc)) from None

        def _chunks():
            try:
                with open(dest, "rb") as fh:
                    while chunk := fh.read(SNAPSHOT_CHUNK_BYTES):
                        yield chunk
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        return StreamingResponse(_chunks(), media_type="application/octet-stream")

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
        reason = data.get("reason")
        if reason is not None and reason not in REJECTION_REASONS:
            # A closed vocabulary (only these three reasons can appear on a
            # rejected row), so a typo is a caller error — 400 up front, not
            # a silent `{"requeued": 0}` that reads as "already handled",
            # and not the 404 the generic ValueError mapping below implies.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown rejection reason {reason!r}; expected one of "
                    f"{sorted(REJECTION_REASONS)}"
                ),
            )
        with session_scope() as session:
            try:
                return _emit.requeue_rejected_bulk(
                    session,
                    subscription_id,
                    event_type=data.get("event_type"),
                    reason=reason,
                )
            except _emit.RequeueRefusedError as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from None
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None

    return router
