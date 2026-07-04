"""The platform's OWN adoption of the replication contract (spec §8, §9 item
5, issue #81).

Scopes are the shared spine every plugin references by slug — a spoke-authored
scope must exist on the hub before spoke-authored plugin writes referencing it
make sense there (spec §8's motivation). So the platform opts into the SAME SDK
emit/ingest modules it offers plugins, rather than inventing a separate
mechanism: `scope.created`/`scope.updated` ride the SDK's
`ReplicationSubscription`/`ReplicationOutboxRow`/`ReplicationInboundStream`
tables, stored in the platform's OWN database (adopted into this package's
alembic chain — see the `adopt_replication_contract` migration) — exactly the
shape architecture.md §4 describes for any opted-in plugin.

`scopes.apply_scope_event` is the domain APPLY function (payload in, idempotent
local write out — checklist item 4); this module wires it into the SDK's
tailnet-gated HTTP surface (`build_replication_router`, spec §5) and exposes
the delivery-loop coroutine `app.py` starts in its lifespan — the same two
things any opted-in plugin does to finish adopting the contract.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from fastapi import APIRouter, Request
from sqlalchemy.orm import Session

from snowline_platform import scopes
from snowline_platform.db import session_scope
from snowline_plugin_sdk.contract import (
    CONTRACT_VERSION,
    EVENT_SCOPE_CREATED,
    EVENT_SCOPE_UPDATED,
)
from snowline_plugin_sdk.replication.admin import _require_trusted, build_replication_router

# Where the platform receives PEER instances' signed scope events, and the §5
# admin surface (pairing CLI target, #82) — distinct from the plugin registry's
# `/plugins` namespace and the scope read/resolve API at `/scopes`.
INGEST_PATH = "/replication/events/ingest"
ADMIN_PREFIX = "/replication-admin"

# The platform's replication SELF-MANIFEST (§8, issue #95). The platform has no
# `/plugins` registry entry of its own, so its scope-stream contract is not
# manifest-discoverable the way a plugin's is — pairing could not read the
# platform's `contract_version`/vocabulary and so SKIPPED the version/vocabulary
# refusal for the scope stream (skew surfaced only later as a delivery-time
# version_hold). This endpoint closes that gap: it self-describes the scope
# stream in the SAME shape as a plugin's manifest `replication` block, so the
# pairing CLI reads it through the identical code path and version-checks the
# platform stream at pairing time exactly like a plugin's.
MANIFEST_PATH = "/replication/manifest"

# The platform's scope-stream event vocabulary (§8) — the events its
# `apply_scope_event` handles and its emit side produces.
SCOPE_EVENTS: tuple[str, ...] = (EVENT_SCOPE_CREATED, EVENT_SCOPE_UPDATED)


def manifest_payload() -> dict:
    """The platform scope stream's self-manifest, same shape as a plugin's
    `replication` block. `contract_version` is the SDK envelope contract the
    platform's own emit/ingest speak (both instances run identical platform
    code, so a skew here is a real deploy skew worth refusing at pairing).
    `advertised_base_url` is absent: a peer discovers the platform AT its
    reachable base URL, so the scope stream carries no distinct advertised
    address — the field is present for shape-parity with the plugin block."""
    return {
        "contract_version": CONTRACT_VERSION,
        "ingest_path": INGEST_PATH,
        "events": list(SCOPE_EVENTS),
        "advertised_base_url": None,
    }


def build_router(
    session_scope_fn: Callable[[], AbstractContextManager[Session]] = session_scope,
) -> APIRouter:
    """The platform's replication HTTP surface: POST `INGEST_PATH` plus the §5
    admin routes (create/list/retire inbound registrations + outbound
    subscriptions, rotation, the parked-events read) — identical shape to what
    any opted-in plugin mounts (§4/§5). `session_scope_fn` is injectable for
    tests; production (`app.py`) uses the platform's own `session_scope`.

    Also mounts the §8 replication self-manifest (`MANIFEST_PATH`, issue #95) —
    a plugin declares its contract in its `/plugins` manifest block, but the
    platform has no registry entry, so it self-describes here for the pairing
    CLI to version-check the scope stream like a plugin's."""
    router = build_replication_router(
        session_scope_fn,
        scopes.apply_scope_event,
        ingest_path=INGEST_PATH,
        admin_prefix=ADMIN_PREFIX,
    )

    @router.get(MANIFEST_PATH)
    async def replication_manifest(request: Request) -> dict:
        # Tailnet-gated with the SAME `_require_trusted` gate as every sibling
        # route in this router (ingest + the admin surface), so the pairing CLI
        # reads it over the trusted tailnet and an untrusted peer gets the same
        # 403 (§5.1). It reveals only the advisory contract metadata a plugin's
        # /plugins manifest entry already exposes — no secrets, no state.
        _require_trusted(request)
        return manifest_payload()

    return router


# The production router — built at import time (routes only; no I/O happens
# until a request arrives), mirroring how `scopes_routes.router` is built.
router = build_router()
