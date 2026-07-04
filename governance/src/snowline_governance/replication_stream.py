"""Governance's #77 STREAM-contract emit side — the full-write-surface event
payloads and the one emit hook (replication-continuity §4 / §9 item 3, #79).

This is the replication-class emit half the spec's §3.2 amendment called for:
each lifecycle write in `decisions` / `shadow` / `artifacts` / `graduation`
builds its domain payload here and calls `emit`, which hands it to the SDK's
`snowline_plugin_sdk.replication.emit.emit_event` — emit-time `seq`, streams
keyed `(source_id, epoch)`, `peer_seen` stamped from the inbound applied
frontier, the whole v2 envelope frozen into the transactional outbox IN the
domain write's transaction. The fire-and-forget webhook bus (`replication.py`)
is UNCHANGED and stays decisions-only; replication-class subscriptions live in
the SDK tables and are managed over the §5 admin surface.

Origin suppression is the SDK's (`emit_event` no-ops while the ingest apply
path runs), and the apply path (`replication_apply`) writes rows DIRECTLY —
never through the emitting services — so an ingest-applied write structurally
cannot re-emit on either bus.

Payload shape: the plugin's domain body the SDK nests whole under the
envelope's `"payload"` key. Every payload carries:

  * `event_id` — a fresh UUID naming THIS lifecycle write; the §6 conflict log
    cites both sides' `event_id`s, and the LWW registers store it as the
    current value's provenance.
  * `at` — the write's authoring timestamp (naive UTC ISO, the house
    convention; both instances are NTP-synced owner Macs, §6). This is the LWW
    comparison key for register-class events.
  * the row's fields, ids included — apply preserves authoring UUIDs so the
    two stores converge byte-for-byte on the rows themselves.

The conversation payload deliberately OMITS the per-branch `seq`: seq is a
local presentation/resume cursor allocated under the branch row lock, so apply
re-allocates it locally (the message SET converges; the interleaving of
messages authored concurrently on both sides is presentation-local).

LWW REGISTERS (§6): the register-class events (`shadow.notes_set`,
`artifact.maturity_set`, `artifact.governs_set`, and node-level
`shadow.graduated`) mutate a row in place, so `emit` also records the write's
`(at, source_id, event_id)` coordinate in `LwwRegister` — the pure-function
input both sides need to pick the same winner when the same object was written
on both sides of a partition. Register upkeep runs even with no subscription
(cheap, and correct the moment pairing happens).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from snowline_plugin_sdk.replication import emit as sdk_emit

from snowline_governance.contract import (
    EVENT_ARTIFACT_GOVERNS_SET,
    EVENT_ARTIFACT_MATURITY_SET,
    EVENT_SHADOW_GRADUATED,
    EVENT_SHADOW_NOTES_SET,
)
from snowline_governance.models import LwwRegister

log = logging.getLogger("snowline.governance.replication_stream")

# The manifest's declared ingest route (§4) — the SDK router serves it and
# `registration.build_manifest` advertises it; one constant so they can't skew.
INGEST_PATH = "/events/ingest"

# event_type → (object_kind, payload id key, field) for the register-class
# events. `shadow.graduated` is register-class ONLY in its node-level shape
# (branch-level stamps a decision each side minted itself — no shared row).
LWW_REGISTERS: dict[str, tuple[str, str, str]] = {
    EVENT_SHADOW_NOTES_SET: ("shadow_branch", "branch_id", "narrative_notes"),
    EVENT_ARTIFACT_MATURITY_SET: ("artifact", "artifact_id", "maturity"),
    EVENT_ARTIFACT_GOVERNS_SET: ("artifact", "artifact_id", "governs"),
    EVENT_SHADOW_GRADUATED: ("shadow_node", "node_id", "graduated_decision_id"),
}


def source_id() -> str:
    """This instance's authoring identity for the LWW registers — lenient
    (defaults `governance`, matching the bus's `_source_id`), unlike the SDK's
    fail-loud pairing-time read: registers must record local writes on an
    unpaired instance too, and unpaired instances never exchange events, so the
    default can't collide with anything."""
    return os.environ.get("SNOWLINE_REPLICATION_SOURCE_ID", "governance")


def utcnow() -> datetime:
    """Naive UTC — the house convention for stored/compared datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_at(value: str) -> datetime:
    """A payload `at`/timestamp back to the naive-UTC datetime the registers
    compare. Tolerates an aware ISO string (normalized to UTC then stripped)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _base(payload: dict) -> dict:
    return {"event_id": str(uuid.uuid4()), "at": utcnow().isoformat(), **payload}


def emit(session, event_type: str, payload: dict) -> list[dict]:
    """The ONE stream-emit hook: write the SDK outbox rows for `payload` (a
    builder's dict, in the caller's transaction) and keep the §6 LWW register
    for register-class events. Returns the SDK's envelopes (empty when nothing
    matched or emission is suppressed — the register upkeep still runs; it
    records LOCAL authorship, not delivery).

    MONOTONIC CLAMP (register-class only): a local write whose wall-clock `at`
    does not EXCEED the register's current coordinate gets bumped strictly past
    it — in the PAYLOAD, before the envelope freezes, so the clamped value is
    what replicates. Two failure modes this closes, both permanent divergence
    under strict LWW: an NTP step-back (the same source writes A at t10 then B
    at "t5" — locally B stands, but B's event would lose to A's everywhere
    else), and overwriting a just-applied peer value during clock skew (the
    local author SAW the value it overwrote, so its write must beat it —
    last-writer means last-observed-writer, not fastest clock). The apply path
    never needs the clamp: a winning apply's coordinate is already >= the
    register's by the LWW rule itself."""
    register = LWW_REGISTERS.get(event_type)
    key = None
    if register is not None:
        kind, id_key, field = register
        object_id = payload.get(id_key)
        if object_id is not None:  # branch-level shadow.graduated has no node_id
            key = (kind, uuid.UUID(str(object_id)), field)
            row = session.get(LwwRegister, key)
            at = parse_at(payload["at"])
            if row is not None and at <= row.written_at:
                payload["at"] = (
                    row.written_at + timedelta(microseconds=1)
                ).isoformat()
    envelopes = sdk_emit.emit_event(session, event_type, payload)
    if key is not None:
        record_register(
            session,
            *key,
            at=parse_at(payload["at"]),
            source=source_id(),
            event_ref=payload["event_id"],
        )
    return envelopes


def record_register(
    session,
    kind: str,
    object_id: uuid.UUID,
    field: str,
    *,
    at: datetime,
    source: str,
    event_ref: str,
) -> None:
    """Upsert one LWW register row to the given authoring coordinate (shared by
    local writes here and winning applies in `replication_apply`)."""
    row = session.get(LwwRegister, (kind, object_id, field))
    if row is None:
        session.add(
            LwwRegister(
                object_kind=kind,
                object_id=object_id,
                field=field,
                written_at=at,
                source_id=source,
                event_ref=event_ref,
            )
        )
    else:
        row.written_at = at
        row.source_id = source
        row.event_ref = event_ref
    session.flush()


# --- payload builders (one per lifecycle write) -------------------------------
#
# Each returns the domain body for one event — row fields with authoring UUIDs
# and timestamps preserved, so apply reconstructs the identical row. Builders
# run AFTER the row is flushed (server defaults populated).


def decision_payload(row) -> dict:
    """`decision.recorded` / `decision.superseded`. Carries the soft scope ref
    BOTH halves (`scope_id` for the stable-id keying #11, `scope` for the §6.1
    ancestors walk + display)."""
    return _base(
        {
            "id": str(row.id),
            "scope_id": str(row.scope_id),
            "scope": row.scope_slug,
            "decision": row.decision,
            "rationale": row.rationale,
            "recorded_at": (
                row.recorded_at.isoformat() if row.recorded_at else None
            ),
            "supersedes_id": (
                str(row.supersedes_id) if row.supersedes_id else None
            ),
        }
    )


def branch_created_payload(branch) -> dict:
    return _base(
        {
            "id": str(branch.id),
            "scope_id": str(branch.scope_id),
            "scope": branch.scope_slug,
            "name": branch.name,
            "narrative_notes": branch.narrative_notes,
            "created_at": (
                branch.created_at.isoformat() if branch.created_at else None
            ),
        }
    )


def branch_archived_payload(branch) -> dict:
    return _base(
        {
            "branch_id": str(branch.id),
            "archived_at": (
                branch.archived_at.isoformat() if branch.archived_at else None
            ),
        }
    )


def notes_set_payload(branch) -> dict:
    return _base(
        {"branch_id": str(branch.id), "narrative_notes": branch.narrative_notes}
    )


def node_added_payload(node) -> dict:
    return _base(
        {
            "id": str(node.id),
            "branch_id": str(node.branch_id),
            "statement": node.statement,
            "rationale": node.rationale,
            "created_at": (
                node.created_at.isoformat() if node.created_at else None
            ),
        }
    )


def citation_added_payload(citation) -> dict:
    return _base(
        {
            "id": str(citation.id),
            "node_id": str(citation.node_id),
            "cited_node_id": (
                str(citation.cited_node_id) if citation.cited_node_id else None
            ),
            "cited_decision_id": (
                str(citation.cited_decision_id)
                if citation.cited_decision_id
                else None
            ),
            "created_at": (
                citation.created_at.isoformat() if citation.created_at else None
            ),
        }
    )


def conversation_appended_payload(event) -> dict:
    """The conversation event WITHOUT its per-branch `seq` (re-allocated at
    apply — see the module docstring)."""
    return _base(
        {
            "id": str(event.id),
            "branch_id": str(event.branch_id),
            "kind": event.kind,
            "payload": event.payload,
            "created_at": (
                event.created_at.isoformat() if event.created_at else None
            ),
        }
    )


def graduated_payload(
    decision_id: str,
    node_id: str | None,
    label: str,
    kind: str | None,
) -> dict:
    """`shadow.graduated` — the provenance stamp AFTER the graduation's own
    `decision.recorded` (same stream, later seq, so apply order is guaranteed).
    Node-level carries `node_id` (and LWW-guards the node's
    `graduated_decision_id` — both sides may graduate the same node across a
    partition); branch-level (end decision / rejection) carries `node_id=None`
    and a discriminating `kind`."""
    return _base(
        {
            "decision_id": decision_id,
            "node_id": node_id,
            "label": label,
            "kind": kind,
        }
    )


def _governs_rows(session, artifact_id) -> list[dict]:
    from sqlalchemy import select

    from snowline_governance.models import ArtifactGoverns

    rows = session.execute(
        select(ArtifactGoverns.scope_id, ArtifactGoverns.scope_slug)
        .where(ArtifactGoverns.artifact_id == artifact_id)
        .order_by(ArtifactGoverns.scope_slug.asc())
    ).all()
    return [{"scope_id": str(sid), "scope": slug} for sid, slug in rows]


def artifact_registered_payload(session, artifact, version) -> dict:
    """`artifact.registered` — the artifact node + its initial version + the
    governs state, one creation write."""
    return _base(
        {
            "id": str(artifact.id),
            "doc_kind": artifact.doc_kind,
            "backend": artifact.backend,
            "maturity": artifact.maturity,
            "governs_all": artifact.governs_all,
            "governs": _governs_rows(session, artifact.id),
            "created_at": (
                artifact.created_at.isoformat() if artifact.created_at else None
            ),
            "version": {
                "id": str(version.id),
                "body_snapshot": version.body_snapshot,
                "created_at": (
                    version.created_at.isoformat()
                    if version.created_at
                    else None
                ),
            },
        }
    )


def artifact_revised_payload(version) -> dict:
    return _base(
        {
            "artifact_id": str(version.artifact_id),
            "version": {
                "id": str(version.id),
                "supersedes_id": (
                    str(version.supersedes_id) if version.supersedes_id else None
                ),
                "relation": version.relation,
                "body_snapshot": version.body_snapshot,
                "summary": version.summary,
                "created_at": (
                    version.created_at.isoformat()
                    if version.created_at
                    else None
                ),
            },
        }
    )


def artifact_resolved_payload(artifact_id, version_id) -> dict:
    return _base(
        {"artifact_id": str(artifact_id), "version_id": str(version_id)}
    )


def maturity_set_payload(artifact) -> dict:
    return _base({"artifact_id": str(artifact.id), "maturity": artifact.maturity})


def governs_set_payload(session, artifact) -> dict:
    return _base(
        {
            "artifact_id": str(artifact.id),
            "governs_all": artifact.governs_all,
            "governs": _governs_rows(session, artifact.id),
        }
    )
