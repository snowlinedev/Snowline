"""The contract-version-2 stream envelope — shapes, signing, and the ingest
response vocabulary (replication-continuity spec §3.2, issue #77).

A stream is `(source_id, epoch)`: `source_id` = `<instance>.<plugin>`, `epoch`
minted at pairing and re-minted at every re-pair/re-seed. The v2 envelope pins:

  * `seq` — allocated at EMIT time, in the domain write's transaction (a
    per-stream counter; see `emit.emit_event`). Authoring order, not delivery
    order — the §3.2 amendment to the bus's delivery-time seq.
  * `peer_seen` — the contiguous APPLIED frontier (`applied_seq`, §3.2) of the
    author's inbound stream from the receiver, stamped at emit. This is what
    makes concurrency computable (§6.1) instead of guessed from wall clocks.
  * `payload` — the plugin's domain body, opaque to the SDK.

Signatures stay DELIVERY-time over the exact bytes POSTed (the bus's existing
behavior) — §5's hitless rotation depends on it: after a secret swap the entire
queued backlog re-signs with the new secret. Sign-time is CONTRACT, not
implementation detail.

The ingest response vocabulary (§3.1/§8.1) — the delivery loop classifies by
HTTP status:
  * 2xx ACKs — `applied`, `duplicate` (seq at/under the delivery gate), and
    `parked` (§8.1: a park ACKs exactly like a success, so the sender's cursor
    advances past the parked seq).
  * 409 REFUSALS — retryable by definition, NEVER dead-lettered: `out_of_order`
    ("expected seq N" — the §3.2 contiguity gate) and `version_hold` (§3.2
    version skew on a live stream is a hold, not a failure).
  * 4xx REJECTIONS (400/401/404) — a delivered event the receiver refused
    (`malformed_envelope`, `bad_signature`, `unknown_stream`): a bug, not a
    partition; the sender dead-letters it (§3.1). The sender dead-letters
    ONLY on this exact vocabulary — a `{"status": "rejected"}` JSON body with
    a reason in `REJECTION_REASONS`. A bare 4xx (a trust-gate 403 — §5.1's own
    config trap, a 404 from a not-yet-mounted router mid-§7-seed, a proxy's
    405/429) never came from a healthy ingest's verdict on THIS event; it is a
    stream-level, usually-recoverable condition and is held retryably —
    otherwise one misconfiguration would cascade-reject the whole backlog,
    one head per tick.
  * 503 RETRY — a bounded retryable apply error (`apply_failed`, §8.1): the
    sender backs off and redelivers; the receiver parks after the bound.

Pure stdlib — no third-party imports (this module is safe from any context; the
sqlalchemy-backed machinery lives in `emit`/`ingest`).
"""

from __future__ import annotations

import hashlib
import hmac

from snowline_plugin_sdk.contract import CONTRACT_VERSION

# --- ingest response vocabulary ----------------------------------------------

STATUS_APPLIED = "applied"
STATUS_DUPLICATE = "duplicate"
STATUS_PARKED = "parked"

REFUSAL_OUT_OF_ORDER = "out_of_order"
REFUSAL_VERSION_HOLD = "version_hold"

RETRY_APPLY_FAILED = "apply_failed"

REJECT_MALFORMED = "malformed_envelope"
REJECT_BAD_SIGNATURE = "bad_signature"
REJECT_UNKNOWN_STREAM = "unknown_stream"

# The sender's dead-letter gate: ONLY a 4xx carrying `{"status": "rejected"}`
# with one of these reasons is a rejection; any other 4xx is a stream-level
# retryable hold (see the module docstring's cascade rationale).
REJECTION_REASONS = frozenset(
    {REJECT_MALFORMED, REJECT_BAD_SIGNATURE, REJECT_UNKNOWN_STREAM}
)

# The envelope fields the v2 stream contract requires beyond the domain payload.
STREAM_FIELDS = ("source", "epoch", "seq", "peer_seen")


def build_envelope(
    event_type: str,
    payload: dict,
    *,
    source_id: str,
    epoch: str,
    seq: int,
    peer_seen: int,
) -> dict:
    """The v2 stream envelope for one event. `payload` is the plugin's domain
    body, nested whole under `"payload"` (the SDK never reads inside it). The
    stream-keying fields (`source`/`epoch`/`seq`) and the causal context
    (`peer_seen`) are envelope-level, alongside the pinned `contract_version`."""
    return {
        "event_type": event_type,
        "contract_version": CONTRACT_VERSION,
        "source": source_id,
        "epoch": epoch,
        "seq": seq,
        "peer_seen": peer_seen,
        "payload": payload,
    }


def sign_body(secret: str, body: bytes) -> str:
    """HMAC-SHA256 of the raw request body under the stream's shared secret,
    hex-encoded — byte-identical to governance `replication.sign` / the SDK's
    `verify_event` recomputation. MUST be computed over the EXACT bytes POSTed
    (delivery-time signing, §5)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time verify of an `X-Snowline-Signature` header (either the
    `sha256=<hex>` form or a bare `<hex>`) against the raw body bytes. False —
    never raises — on a missing/malformed header, so a bad header is a clean
    401, not a 500 (the monolith `replication_ingest.verify_signature` shape)."""
    if not signature:
        return False
    provided = (
        signature[len("sha256=") :] if signature.startswith("sha256=") else signature
    )
    return hmac.compare_digest(provided, sign_body(secret, body))


# --- the §7.1 backfill stream identity ----------------------------------------
#
# A cold-start union (spec §7.1, #98) replays one instance's domain rows to its
# peer as synthetic events on an ORDINARY stream with a reserved identity:
# `source_id = <live source_id>#backfill`. The suffix is the ONLY marker a
# backfill envelope carries — no extra envelope field, no contract bump — and it
# is load-bearing three ways: the live and backfill streams coexist under the
# §5 single-active-stream-per-source invariant (different sources), the same
# invariant applied to the backfill id yields "one backfill per direction at a
# time", and `peer_seen`'s live lookup can never match a backfill stream.

BACKFILL_SUFFIX = "#backfill"


def backfill_source_id(live_source_id: str) -> str:
    """The reserved backfill stream identity for a live `source_id`
    (`mini.governance` → `mini.governance#backfill`). Idempotent on an id that
    already carries the suffix."""
    if is_backfill_source(live_source_id):
        return live_source_id
    return f"{live_source_id}{BACKFILL_SUFFIX}"


def is_backfill_source(source_id: str | None) -> bool:
    """True iff `source_id` names a §7.1 backfill stream."""
    return isinstance(source_id, str) and source_id.endswith(BACKFILL_SUFFIX)


def live_source_id(source_id: str | None) -> str | None:
    """`source_id` with any `#backfill` suffix stripped — the normalization an
    apply performs before recording an envelope's source into DOMAIN state (§6's
    LWW registers, §7.1 provenance), so register state stays byte-identical
    across instances whichever way the winning event arrived."""
    if is_backfill_source(source_id):
        return source_id[: -len(BACKFILL_SUFFIX)]
    return source_id


def is_backfill_envelope(envelope: dict) -> bool:
    """The apply-seam hook (§7.1): True iff this envelope arrived on a backfill
    stream. The applies that behave differently under a union (the platform's
    scope convergence, governance's §6.1 detection inputs) key off this and
    NOTHING else; every other apply is deliberately identical for live and
    backfilled events."""
    return is_backfill_source((envelope or {}).get("source"))
