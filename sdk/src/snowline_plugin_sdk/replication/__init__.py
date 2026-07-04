"""SDK replication modules — the envelope mechanics of plugin-owned event
replication (replication-continuity spec §3, issue #77).

The SDK owns the *envelope*: the transactional outbox + per-stream emit-time
`seq` allocation (`emit`), the delivery loop with the replication retry class
(unbounded retry, capped per-row backoff, per-ingest reachability probe — §3.1),
the per-stream watermark + contiguous-apply gate, signature verify, origin
suppression, and parking (`ingest`, §3.2/§8.1), plus the tailnet-gated
replication-admin surface + ingest route (`admin`, §5). The PLUGIN owns the
*domain*: it supplies the idempotent apply function and calls `emit_event` from
its domain writes. A plugin opts in by adopting these modules, not by rewriting
replication (§4).

Import discipline: this subpackage pulls `sqlalchemy` (and `admin` pulls
`fastapi`), so it rides the SDK's `[replication]` extra and is imported
EXPLICITLY (`from snowline_plugin_sdk.replication import ...`) — the SDK package
ROOT stays import-pure (stdlib only), same posture as `.registration`'s
`[client]` extra. `admin` is deliberately NOT re-exported here so emit/ingest
stay usable without fastapi installed.
"""

from .emit import (
    create_outbound_subscription,
    deliver_pending,
    emit_event,
    list_outbound_subscriptions,
    list_rejected,
    replication_delivery_loop,
    requeue_rejected,
    retire_outbound_subscription,
    set_subscription_secret,
)
from .envelope import build_envelope, sign_body, verify_signature
from .ingest import (
    ParkNow,
    ingest_delivery,
    is_applying_replicated_event,
    list_inbound_streams,
    list_parked,
    reapply_parked,
    register_inbound_stream,
    retire_inbound_stream,
    rotate_inbound_secret,
)
from .models import (
    ReplicationBase,
    ReplicationInboundStream,
    ReplicationOutboxRow,
    ReplicationParkedEvent,
    ReplicationStreamCounter,
    ReplicationSubscription,
)

__all__ = [
    "ReplicationBase",
    "ReplicationSubscription",
    "ReplicationOutboxRow",
    "ReplicationStreamCounter",
    "ReplicationInboundStream",
    "ReplicationParkedEvent",
    "build_envelope",
    "sign_body",
    "verify_signature",
    "emit_event",
    "create_outbound_subscription",
    "list_outbound_subscriptions",
    "retire_outbound_subscription",
    "set_subscription_secret",
    "deliver_pending",
    "replication_delivery_loop",
    "list_rejected",
    "requeue_rejected",
    "ingest_delivery",
    "register_inbound_stream",
    "list_inbound_streams",
    "retire_inbound_stream",
    "rotate_inbound_secret",
    "reapply_parked",
    "list_parked",
    "is_applying_replicated_event",
    "ParkNow",
]
