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

from fastapi import APIRouter
from sqlalchemy.orm import Session

from snowline_platform import scopes
from snowline_platform.db import session_scope
from snowline_plugin_sdk.replication.admin import build_replication_router

# Where the platform receives PEER instances' signed scope events, and the §5
# admin surface (pairing CLI target, #82) — distinct from the plugin registry's
# `/plugins` namespace and the scope read/resolve API at `/scopes`.
INGEST_PATH = "/replication/events/ingest"
ADMIN_PREFIX = "/replication-admin"


def build_router(
    session_scope_fn: Callable[[], AbstractContextManager[Session]] = session_scope,
) -> APIRouter:
    """The platform's replication HTTP surface: POST `INGEST_PATH` plus the §5
    admin routes (create/list/retire inbound registrations + outbound
    subscriptions, rotation, the parked-events read) — identical shape to what
    any opted-in plugin mounts (§4/§5). `session_scope_fn` is injectable for
    tests; production (`app.py`) uses the platform's own `session_scope`."""
    return build_replication_router(
        session_scope_fn,
        scopes.apply_scope_event,
        ingest_path=INGEST_PATH,
        admin_prefix=ADMIN_PREFIX,
    )


# The production router — built at import time (routes only; no I/O happens
# until a request arrives), mirroring how `scopes_routes.router` is built.
router = build_router()
