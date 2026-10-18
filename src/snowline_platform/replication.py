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

from snowline_platform import milestones, scopes
from snowline_platform.db import session_scope
from snowline_plugin_sdk.contract import (
    CONTRACT_VERSION,
    EVENT_MILESTONE_CREATED,
    EVENT_MILESTONE_DEPENDENCY_CHANGED,
    EVENT_MILESTONE_MERGED,
    EVENT_MILESTONE_TRANSITIONED,
    EVENT_MILESTONE_UPDATED,
    EVENT_SCOPE_CREATED,
    EVENT_SCOPE_UPDATED,
)
from snowline_plugin_sdk.replication.admin import _require_trusted, build_replication_router
from snowline_plugin_sdk.replication.skew import KeyInventory

# N-1 skew guard (decision 54447516, #216): the payload-key inventory of the
# platform's own stream - scope.* (spec §8) and milestone.* (milestones.md §9).
# PAYLOAD_KEYS = the baseline every N-1 peer sends; WIDENED_KEYS = keys added
# since, which apply reads ONLY on presence (`"k" in payload` / `.get`);
# OPTIONAL_KEYS = conditional keys (the §7.1 backfill enumerator's per-register
# author stamps), allowed but never required, also read only on presence. A new
# payload key lands in WIDENED_KEYS with its guard and folds into PAYLOAD_KEYS
# only after one train ships. Enforced by tests/test_replication_skew.py
# (emitted keys == declared; every widened/optional key guarded in scopes.py /
# milestones.py apply). Helper: snowline_plugin_sdk.replication.skew.
#
# Provenance: the baseline is train v0.4.0 (tag 85e7858, 2026-09-13). The
# widened milestone keys first shipped in v0.5.0 - line_rank (#266), title
# (#278), descriptive_authored_at / lifecycle_authored_at (#270) - so they are
# foldable after the NEXT train ships and the spoke has moved off v0.4.0. Scope
# created_at / updated_at (#284) are unreleased.
_SCOPE_BASE = {"id", "slug", "name", "kind", "parent", "isolated", "status"}
_MILESTONE_ROW = {
    "address", "anchor", "name", "outcome", "status", "target_date",
    "activated_at", "achieved_at", "cancelled_at", "merged_into", "authored_at",
}
_MILESTONE_ROW_WIDENED = {
    "line_rank", "title", "descriptive_authored_at", "lifecycle_authored_at",
}
PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    EVENT_SCOPE_CREATED: frozenset(_SCOPE_BASE),
    EVENT_SCOPE_UPDATED: frozenset(_SCOPE_BASE),
    EVENT_MILESTONE_CREATED: frozenset(_MILESTONE_ROW),
    EVENT_MILESTONE_UPDATED: frozenset(_MILESTONE_ROW),
    # `warnings`: persisted transition warnings (#159), baseline since v0.4.0.
    EVENT_MILESTONE_TRANSITIONED: frozenset(
        _MILESTONE_ROW | {"from_status", "to_status", "reason", "warnings"}
    ),
    EVENT_MILESTONE_DEPENDENCY_CHANGED: frozenset(
        {"op", "dependent", "dependency", "authored_at"}
    ),
    EVENT_MILESTONE_MERGED: frozenset({"from", "into", "authored_at"}),
}
WIDENED_KEYS: dict[str, frozenset[str]] = {
    # created_at / updated_at: the scope LWW clock (spec §7.1, additive).
    EVENT_SCOPE_CREATED: frozenset({"created_at", "updated_at"}),
    EVENT_SCOPE_UPDATED: frozenset({"created_at", "updated_at"}),
    EVENT_MILESTONE_CREATED: frozenset(_MILESTONE_ROW_WIDENED),
    EVENT_MILESTONE_UPDATED: frozenset(_MILESTONE_ROW_WIDENED),
    EVENT_MILESTONE_TRANSITIONED: frozenset(_MILESTONE_ROW_WIDENED),
    EVENT_MILESTONE_DEPENDENCY_CHANGED: frozenset(),
    EVENT_MILESTONE_MERGED: frozenset(),
}
# Conditional keys added ONLY by `milestones.enumerate_backfill` (§7.1
# cold-start union, #284), where the row stores a per-register author; apply
# reads them through `_event_source` -> `payload.get(key)`.
OPTIONAL_KEYS: dict[str, frozenset[str]] = {
    EVENT_MILESTONE_CREATED: frozenset({"descriptive_source_id", "lifecycle_source_id"}),
    EVENT_MILESTONE_TRANSITIONED: frozenset(
        {"descriptive_source_id", "lifecycle_source_id", "transition_source_id"}
    ),
    EVENT_MILESTONE_MERGED: frozenset({"source_id"}),
}
KEY_INVENTORY = KeyInventory(PAYLOAD_KEYS, WIDENED_KEYS, OPTIONAL_KEYS)

# Where the platform receives PEER instances' signed scope events, and the §5
# admin surface (pairing CLI target, #82) — distinct from the plugin registry's
# `/plugins` namespace and the scope read/resolve API at `/scopes`.
INGEST_PATH = "/replication/events/ingest"
ADMIN_PREFIX = "/replication-admin"

# Replication VIA the gateway (governance decision 0b8390f7, item 6754a127):
# a PEER instance reaches this instance's plugins' replication surfaces at
# `<platform_url>/via/<plugin><ingest_path | admin path>` — the gateway proxies
# to the plugin's loopback `base_url` (`http_proxy.ReplicationViaProxy`). The
# platform's OWN scope stream needs no `/via`: it is served at `platform_url`
# directly (INGEST_PATH / ADMIN_PREFIX above). The only tailnet-reachable port
# per instance is therefore the platform's; plugins keep binding loopback and
# never need a per-port `tailscale serve` mirror. Shared by the proxy (serves
# it), pairing (addresses peers with it) and the stack model (seeds with it).
VIA_PREFIX = "/via"


def via_base(platform_url: str, plugin_name: str) -> str:
    """The ONE join for "this plugin, through that gateway": the base a peer
    addresses `plugin_name`'s replication surfaces at on the instance whose
    gateway is `platform_url`. Used by pairing (peer discovery), the seed
    (spoke ingest targets) and the stack model (seed.json) alike, so the shape
    cannot drift between them."""
    return f"{platform_url.rstrip('/')}{VIA_PREFIX}/{plugin_name}"

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

# The platform's scope-stream event vocabulary (§8) — the events
# `apply_scope_event` handles and the scope service's emit side produces.
SCOPE_EVENTS: tuple[str, ...] = (EVENT_SCOPE_CREATED, EVENT_SCOPE_UPDATED)

# The platform's milestone-registry event vocabulary (milestones.md §9, #145) —
# the milestone service emits these and `apply_milestone_event` handles them, on
# the SAME platform stream as scopes (one platform, one replication stream).
MILESTONE_EVENTS: tuple[str, ...] = (
    EVENT_MILESTONE_CREATED,
    EVENT_MILESTONE_UPDATED,
    EVENT_MILESTONE_TRANSITIONED,
    EVENT_MILESTONE_DEPENDENCY_CHANGED,
    EVENT_MILESTONE_MERGED,
)

# The platform's FULL replication vocabulary — scopes + milestones. This is what
# the self-manifest advertises, so a pairing/seed handshake subscribes a peer's
# forward stream to milestone events too (without it, a newly-paired instance
# would receive scope rows but silently never the milestone registry, §9).
PLATFORM_EVENTS: tuple[str, ...] = SCOPE_EVENTS + MILESTONE_EVENTS


def apply_platform_event(session: Session, envelope: dict) -> None:
    """The platform's single replication APPLY seam — dispatches by event type to
    the owning service's apply function (scopes vs milestones), since the platform
    runs ONE inbound stream carrying both namespaces' events. Both are address/
    slug-keyed idempotent applies run under origin suppression (§9)."""
    event_type = envelope.get("event_type")
    if event_type in SCOPE_EVENTS:
        scopes.apply_scope_event(session, envelope)
    elif event_type in MILESTONE_EVENTS:
        milestones.apply_milestone_event(session, envelope)
    else:
        raise ValueError(
            f"platform replication apply: unknown event_type {event_type!r}"
        )


def enumerate_backfill(session: Session):
    """The platform stream's §7.1 enumerator: the scope namespace FIRST
    (`scopes.enumerate_backfill`, parents first), then the milestone namespace
    (`milestones.enumerate_backfill`) — one stream, so a milestone's anchor
    slug always resolves on the receiver before the milestone arrives, and the
    plugin backfills that follow (§7.1 step 3) find every scope present."""
    yield from scopes.enumerate_backfill(session)
    yield from milestones.enumerate_backfill(session)


def manifest_payload() -> dict:
    """The platform stream's self-manifest, same shape as a plugin's `replication`
    block. `events` is the FULL platform vocabulary — scopes AND milestones (§9) —
    so pairing subscribes a peer to both. `contract_version` is the SDK envelope
    contract the platform's own emit/ingest speak (both instances run identical
    platform code, so a skew here is a real deploy skew worth refusing at
    pairing). `train_version` is the running train (#259, readiness
    surface). `advertised_base_url` is absent: a peer discovers the platform AT
    its reachable base URL, so the stream carries no distinct advertised address —
    the field is present for shape-parity with the plugin block."""
    from snowline_platform.standby import platform_train

    return {
        "contract_version": CONTRACT_VERSION,
        "ingest_path": INGEST_PATH,
        "events": list(PLATFORM_EVENTS),
        "advertised_base_url": None,
        # The release train this platform runs (#259) — `v0.5.1`, `source`
        # for an editable checkout, or None. Additive and advisory: pairing
        # reads only the three required keys, so an older peer ignores it,
        # and a peer's readiness view falls back to it when the peer
        # predates `/standby/local`.
        "train_version": platform_train().get("version"),
    }


def build_router(
    session_scope_fn: Callable[[], AbstractContextManager[Session]] = session_scope,
) -> APIRouter:
    """The platform's replication HTTP surface: POST `INGEST_PATH` plus the §5
    admin routes (create/list/retire inbound registrations + outbound
    subscriptions, rotation, the parked-events read, and the §7 step-2
    `/snapshot` dump of the platform's OWN database) — identical shape to what
    any opted-in plugin mounts (§4/§5). `session_scope_fn` is injectable for
    tests; production (`app.py`) uses the platform's own `session_scope`, whose
    sessions are engine-bound, which is where `/snapshot` reads the database URL
    it hands to `pg_dump` (item 0ebe6a70 / decision 1a83031c — nothing dials the
    hub's Postgres across the tailnet).

    Also mounts the §8 replication self-manifest (`MANIFEST_PATH`, issue #95) —
    a plugin declares its contract in its `/plugins` manifest block, but the
    platform has no registry entry, so it self-describes here for the pairing
    CLI to version-check the scope stream like a plugin's."""
    router = build_replication_router(
        session_scope_fn,
        apply_platform_event,
        ingest_path=INGEST_PATH,
        admin_prefix=ADMIN_PREFIX,
        # §7.1: the platform's union half is BOTH namespaces the stream carries
        # — scopes first (parents first), then milestones (whose anchors are
        # then already on the receiver).
        enumerator=enumerate_backfill,
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
