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
from snowline_plugin_sdk.replication.skew import KeyInventory

from snowline_governance.contract import (
    EVENT_ARTIFACT_CODE_REFS_SET,
    EVENT_ARTIFACT_GOVERNS_SET,
    EVENT_ARTIFACT_MATURITY_SET,
    EVENT_ARTIFACT_REGISTERED,
    EVENT_ARTIFACT_RESOLVED,
    EVENT_ARTIFACT_REVISED,
    EVENT_ARTIFACT_SUPERSEDED,
    EVENT_DECISION_MARKED_COMPATIBLE,
    EVENT_DECISION_RECORDED,
    EVENT_DECISION_SUPERSEDED,
    EVENT_SHADOW_BRANCH_ARCHIVED,
    EVENT_SHADOW_BRANCH_CREATED,
    EVENT_SHADOW_CITATION_ADDED,
    EVENT_SHADOW_CONVERSATION_APPENDED,
    EVENT_SHADOW_GRADUATED,
    EVENT_SHADOW_NODE_ADDED,
    EVENT_SHADOW_NOTES_SET,
)
from snowline_governance.models import LwwRegister

log = logging.getLogger("snowline.governance.replication_stream")

# N-1 skew guard (decision 54447516, #216): the top-level payload-key inventory
# of every governance-owned event. PAYLOAD_KEYS = the baseline every N-1 peer
# sends; WIDENED_KEYS = keys added since, applied only on presence in
# `replication_apply` (declare the key here, guard it there, fold it into
# PAYLOAD_KEYS after one train). Nothing is inside the window today, so every
# widening is empty - the tripwire in tests/test_replication_stream.py stays
# armed for the next one. Nested objects (artifact `version`) are not inventoried.
# Provenance: baseline = train v0.4.0; no widening since.
_ENVELOPE_BODY = {"event_id", "at"}  # stamped on every governance payload
_DECISION_KEYS = {
    "id", "decision", "rationale", "scope", "scope_id", "supersedes_id", "recorded_at",
}
PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    t: frozenset(_ENVELOPE_BODY | keys)
    for t, keys in {
        EVENT_DECISION_RECORDED: _DECISION_KEYS,
        EVENT_DECISION_SUPERSEDED: _DECISION_KEYS,
        EVENT_DECISION_MARKED_COMPATIBLE: {
            "decision_id", "concurrent_with_id", "actor", "marked_compatible_at",
        },
        EVENT_SHADOW_BRANCH_CREATED: {
            "id", "name", "scope", "scope_id", "narrative_notes", "created_at",
        },
        EVENT_SHADOW_BRANCH_ARCHIVED: {"branch_id", "archived_at"},
        EVENT_SHADOW_NOTES_SET: {"branch_id", "narrative_notes"},
        EVENT_SHADOW_NODE_ADDED: {
            "id", "branch_id", "statement", "rationale", "created_at",
        },
        EVENT_SHADOW_CITATION_ADDED: {
            "id", "node_id", "cited_node_id", "cited_decision_id", "created_at",
        },
        EVENT_SHADOW_CONVERSATION_APPENDED: {
            "id", "branch_id", "kind", "payload", "created_at",
        },
        EVENT_SHADOW_GRADUATED: {"decision_id", "node_id", "kind", "label"},
        EVENT_ARTIFACT_REGISTERED: {
            "id", "backend", "doc_kind", "maturity", "governs", "governs_all",
            "created_at", "version",
        },
        EVENT_ARTIFACT_REVISED: {"artifact_id", "version"},
        EVENT_ARTIFACT_RESOLVED: {"artifact_id", "version_id"},
        EVENT_ARTIFACT_MATURITY_SET: {"artifact_id", "maturity"},
        EVENT_ARTIFACT_GOVERNS_SET: {"artifact_id", "governs", "governs_all"},
        EVENT_ARTIFACT_SUPERSEDED: {
            "artifact_id", "superseded_by_id", "superseded_at", "retirement_reason",
        },
        EVENT_ARTIFACT_CODE_REFS_SET: {"artifact_id", "code_refs"},
    }.items()
}
WIDENED_KEYS: dict[str, frozenset[str]] = {t: frozenset() for t in PAYLOAD_KEYS}
# `lww_source`: the register's ORIGINAL author, added ONLY by the §7.1 backfill
# enumerator (`_replay_register`, cold-start union #284) on the register-class
# events; apply reads it via `payload.get("lww_source")` in `_lww_apply`.
OPTIONAL_KEYS: dict[str, frozenset[str]] = {
    t: frozenset({"lww_source"})
    for t in (
        EVENT_SHADOW_NOTES_SET,
        EVENT_ARTIFACT_MATURITY_SET,
        EVENT_ARTIFACT_GOVERNS_SET,
        EVENT_ARTIFACT_SUPERSEDED,
        EVENT_ARTIFACT_CODE_REFS_SET,
        EVENT_SHADOW_GRADUATED,
    )  # == LWW_REGISTERS (asserted in tests)
}
KEY_INVENTORY = KeyInventory(PAYLOAD_KEYS, WIDENED_KEYS, OPTIONAL_KEYS)

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
    EVENT_ARTIFACT_SUPERSEDED: ("artifact", "artifact_id", "superseded_by"),
    EVENT_ARTIFACT_CODE_REFS_SET: ("artifact", "artifact_id", "code_refs"),
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


def marked_compatible_payload(
    lo: uuid.UUID, hi: uuid.UUID, actor: str, marked_at: datetime
) -> dict:
    """`decision.marked_compatible` (#97) — §6.1's explicit compatibility
    judgment. Carries the NORMALIZED immutable pair (lesser UUID first, both
    sides key the identical `DecisionConcurrence` row), the `actor` who judged,
    and `marked_compatible_at` — the permanent stamp apply writes (the earliest
    wins, so this is order-independent). `_base`'s `at` is the occurred-at."""
    return _base(
        {
            "decision_id": str(lo),
            "concurrent_with_id": str(hi),
            "actor": actor,
            "marked_compatible_at": marked_at.isoformat(),
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
                # SOFT milestone ref rides the version half — a replicated version
                # would silently lose its release stamp without it (§4 / §6).
                "milestone": version.milestone,
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
                # SOFT milestone ref rides the version half (§4 / §6) — apply
                # stamps it back, so a replicated revision keeps its release tag.
                "milestone": version.milestone,
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


def artifact_superseded_payload(artifact) -> dict:
    """`artifact.superseded` (#166/#174) — the FULL retirement state as row
    state: the successor pointer (None for a #174 reason-only retirement), the
    reason (None for a pure consolidation), and the stamp. Apply replaces all
    three wholesale, so a partitioned retire-vs-supersede race converges on the
    later write's complete state. `superseded_at` rides as a DOMAIN field
    (stamped back byte-for-byte); the envelope `at` is the LWW coordinate,
    which the emit clamp may nudge — the two are deliberately distinct."""
    return _base(
        {
            "artifact_id": str(artifact.id),
            "superseded_by_id": (
                str(artifact.superseded_by_id)
                if artifact.superseded_by_id
                else None
            ),
            "retirement_reason": artifact.retirement_reason,
            "superseded_at": (
                artifact.superseded_at.isoformat()
                if artifact.superseded_at
                else None
            ),
        }
    )


def code_refs_set_payload(artifact) -> dict:
    """`artifact.code_refs_set` (#172) — the whole normalized list as row state
    (a wholesale replace, so the whole list is the LWW-contested value, exactly
    like governs)."""
    return _base(
        {"artifact_id": str(artifact.id), "code_refs": artifact.code_refs}
    )


def governs_set_payload(session, artifact) -> dict:
    return _base(
        {
            "artifact_id": str(artifact.id),
            "governs_all": artifact.governs_all,
            "governs": _governs_rows(session, artifact.id),
        }
    )


# --- §7.1 backfill enumerator (#98) --------------------------------------------
#
# Governance's half of the cold-start union: a deterministic walk of the WHOLE
# replicated row surface, yielding `(event_type, payload)` pairs from the
# EXISTING vocabulary (no new event shapes) such that applying them to an empty
# peer reproduces every row's current state, and applying them to a peer that
# already holds a row is the ordinary idempotent / LWW path.
#
# Rules with teeth (spec §7.1):
#   * LIFECYCLE STATE is enumerated, not just live rows — a superseding decision
#     replays as `decision.superseded` (its edge), an archived branch replays
#     its archival, a retired artifact its retirement, a resolved version its
#     flip; a walk of "current" rows only would resurrect or orphan history.
#   * Where one event can't carry a row's final state, the MINIMAL lifecycle
#     sequence is emitted (artifact: registered → revised… → resolved → its
#     register-class events).
#   * Timestamps are ORIGINAL authored-at times, never import time. Register-
#     class events replay the `LwwRegister` coordinate itself — `at`,
#     `event_id` and the authoring source (`lww_source`, ADDITIVE: the sender
#     may hold a value written by a third party or by the receiver itself, so
#     the stream source can't stand in for it) — so register state converges
#     byte-identically and a re-run ties instead of winning.
#   * Non-register events get a DETERMINISTIC `event_id` (uuid5 of the event
#     and row), so a re-walk yields byte-identical payloads.
#
# Walk order is fixed per domain table by (authored_at, id), with FK
# predecessors first (supersession parents before children, artifact roots
# before revisions, every node before any citation, decisions + nodes before
# graduation stamps, every artifact before any retirement pointer).

_BACKFILL_NS = uuid.UUID("5d7e3a0c-98a1-4f2b-9b61-2f0f5c9e7a11")


def _replay(payload: dict, event_type: str, row_id, at: datetime | None) -> dict:
    """Pin a builder's fresh `event_id`/`at` to deterministic, ORIGINAL ones."""
    payload["event_id"] = str(uuid.uuid5(_BACKFILL_NS, f"{event_type}:{row_id}"))
    if at is not None:
        payload["at"] = at.isoformat()
    return payload


def _replay_register(payload: dict, register) -> dict:
    """Pin a register-class payload to the stored LWW coordinate."""
    payload["at"] = register.written_at.isoformat()
    payload["event_id"] = register.event_ref
    payload["lww_source"] = register.source_id
    return payload


def _parents_first(rows: list, key_parent) -> list:
    """`rows` (already in (authored_at, id) order) re-ordered so each row
    follows the row its `key_parent` names — deterministic, cycle-safe."""
    by_id = {r.id: r for r in rows}
    out: list = []
    done: set = set()

    def visit(row, chain: set) -> None:
        if row.id in done:
            return
        parent = by_id.get(key_parent(row))
        if parent is not None and parent.id not in chain:
            visit(parent, chain | {parent.id})
        done.add(row.id)
        out.append(row)

    for row in rows:
        visit(row, {row.id})
    return out


def enumerate_backfill(session):
    """Governance's §7.1 enumerator (see the section comment above). Read-only;
    the SDK walker runs it inside one snapshot."""
    from sqlalchemy import select

    from snowline_governance.contract import (
        EVENT_ARTIFACT_REGISTERED,
        EVENT_ARTIFACT_RESOLVED,
        EVENT_ARTIFACT_REVISED,
        EVENT_DECISION_MARKED_COMPATIBLE,
        EVENT_DECISION_RECORDED,
        EVENT_DECISION_SUPERSEDED,
        EVENT_SHADOW_BRANCH_ARCHIVED,
        EVENT_SHADOW_BRANCH_CREATED,
        EVENT_SHADOW_CITATION_ADDED,
        EVENT_SHADOW_CONVERSATION_APPENDED,
        EVENT_SHADOW_NODE_ADDED,
    )
    from snowline_governance.models import (
        SHADOW_BRANCH_STATUS_ARCHIVED,
        Artifact,
        ArtifactVersion,
        Decision,
        DecisionConcurrence,
        ShadowBranch,
        ShadowConversationEvent,
        ShadowNode,
        ShadowNodeCitation,
    )

    def registers(kind: str) -> dict:
        return {
            (r.object_id, r.field): r
            for r in session.scalars(
                select(LwwRegister).where(LwwRegister.object_kind == kind)
            )
        }

    # 1. Decisions — supersession parents first (the child's edge is its
    #    `decision.superseded` append half).
    decisions = _parents_first(
        list(
            session.scalars(select(Decision).order_by(Decision.recorded_at, Decision.id))
        ),
        lambda d: d.supersedes_id,
    )
    for d in decisions:
        event = EVENT_DECISION_SUPERSEDED if d.supersedes_id else EVENT_DECISION_RECORDED
        yield event, _replay(decision_payload(d), event, d.id, d.recorded_at)

    # 2. §6.1 compatibility judgments (the flags themselves are detection-
    #    derived on each side and never replicate; the explicit marks do).
    for c in session.scalars(
        select(DecisionConcurrence)
        .where(DecisionConcurrence.marked_compatible_at.is_not(None))
        .order_by(DecisionConcurrence.marked_compatible_at, DecisionConcurrence.decision_id)
    ):
        payload = marked_compatible_payload(
            c.decision_id, c.concurrent_with_id, "backfill", c.marked_compatible_at
        )
        yield EVENT_DECISION_MARKED_COMPATIBLE, _replay(
            payload,
            EVENT_DECISION_MARKED_COMPATIBLE,
            f"{c.decision_id}:{c.concurrent_with_id}",
            c.marked_compatible_at,
        )

    # 3. Shadow branches: creation, the notes register, archival.
    branch_regs = registers("shadow_branch")
    branches = list(
        session.scalars(select(ShadowBranch).order_by(ShadowBranch.created_at, ShadowBranch.id))
    )
    for b in branches:
        yield EVENT_SHADOW_BRANCH_CREATED, _replay(
            branch_created_payload(b), EVENT_SHADOW_BRANCH_CREATED, b.id, b.created_at
        )
        reg = branch_regs.get((b.id, "narrative_notes"))
        if reg is not None:
            yield EVENT_SHADOW_NOTES_SET, _replay_register(notes_set_payload(b), reg)
        if b.status == SHADOW_BRANCH_STATUS_ARCHIVED:
            yield EVENT_SHADOW_BRANCH_ARCHIVED, _replay(
                branch_archived_payload(b),
                EVENT_SHADOW_BRANCH_ARCHIVED,
                b.id,
                b.archived_at,
            )

    # 4. Nodes, then citations (a citation may name any node), then the
    #    conversation log (seq is presentation-local — apply re-allocates it).
    for n in session.scalars(select(ShadowNode).order_by(ShadowNode.created_at, ShadowNode.id)):
        yield EVENT_SHADOW_NODE_ADDED, _replay(
            node_added_payload(n), EVENT_SHADOW_NODE_ADDED, n.id, n.created_at
        )
    for c in session.scalars(
        select(ShadowNodeCitation).order_by(ShadowNodeCitation.created_at, ShadowNodeCitation.id)
    ):
        yield EVENT_SHADOW_CITATION_ADDED, _replay(
            citation_added_payload(c), EVENT_SHADOW_CITATION_ADDED, c.id, c.created_at
        )
    for e in session.scalars(
        select(ShadowConversationEvent).order_by(
            ShadowConversationEvent.branch_id, ShadowConversationEvent.seq
        )
    ):
        yield EVENT_SHADOW_CONVERSATION_APPENDED, _replay(
            conversation_appended_payload(e),
            EVENT_SHADOW_CONVERSATION_APPENDED,
            e.id,
            e.created_at,
        )

    # 5. Graduation stamps — after their decisions and nodes. A node's CURRENT
    #    pointer replays its register coordinate; every other stamp (a
    #    branch-level graduation, or a node-level one whose pointer lost a
    #    race) replays at its decision's authoring time — its append half
    #    always lands, its pointer mutation yields to the register.
    node_regs = registers("shadow_node")
    nodes = {n.id: n for n in session.scalars(select(ShadowNode))}
    stamps = []
    for d in decisions:
        if d.shadow_origin_label is None:
            continue
        node_id = uuid.UUID(d.shadow_origin_node_id) if d.shadow_origin_node_id else None
        payload = graduated_payload(
            str(d.id),
            str(node_id) if node_id else None,
            d.shadow_origin_label,
            d.shadow_origin_kind,
        )
        node = nodes.get(node_id) if node_id else None
        reg = node_regs.get((node_id, "graduated_decision_id")) if node_id else None
        if node is not None and node.graduated_decision_id == d.id and reg is not None:
            _replay_register(payload, reg)
        else:
            _replay(payload, EVENT_SHADOW_GRADUATED, d.id, d.recorded_at)
        stamps.append(payload)
    for payload in sorted(stamps, key=lambda p: (p["at"], p["decision_id"])):
        yield EVENT_SHADOW_GRADUATED, payload

    # 6. Artifacts: registration (with the ROOT version), revisions (parents
    #    first), resolutions, then the register-class state.
    artifact_regs = registers("artifact")
    artifacts_ = list(
        session.scalars(select(Artifact).order_by(Artifact.created_at, Artifact.id))
    )
    versions = list(
        session.scalars(
            select(ArtifactVersion).order_by(ArtifactVersion.created_at, ArtifactVersion.id)
        )
    )
    by_artifact: dict = {}
    for v in versions:
        by_artifact.setdefault(v.artifact_id, []).append(v)
    roots: dict = {}
    for a in artifacts_:
        vs = by_artifact.get(a.id, [])
        if not vs:
            continue  # an artifact without a version can't be registered on the wire
        root = next((v for v in vs if v.supersedes_id is None), vs[0])
        roots[a.id] = root.id
        yield EVENT_ARTIFACT_REGISTERED, _replay(
            artifact_registered_payload(session, a, root),
            EVENT_ARTIFACT_REGISTERED,
            a.id,
            a.created_at,
        )
    root_ids = set(roots.values())
    for v in _parents_first(versions, lambda v: v.supersedes_id):
        if v.id in root_ids or v.artifact_id not in roots:
            continue
        yield EVENT_ARTIFACT_REVISED, _replay(
            artifact_revised_payload(v), EVENT_ARTIFACT_REVISED, v.id, v.created_at
        )
    for v in versions:
        if v.status == "superseded" and v.artifact_id in roots:
            yield EVENT_ARTIFACT_RESOLVED, _replay(
                artifact_resolved_payload(v.artifact_id, v.id),
                EVENT_ARTIFACT_RESOLVED,
                v.id,
                v.created_at,
            )
    builders = (
        ("maturity", EVENT_ARTIFACT_MATURITY_SET, lambda a: maturity_set_payload(a)),
        ("governs", EVENT_ARTIFACT_GOVERNS_SET, lambda a: governs_set_payload(session, a)),
        ("code_refs", EVENT_ARTIFACT_CODE_REFS_SET, lambda a: code_refs_set_payload(a)),
        ("superseded_by", EVENT_ARTIFACT_SUPERSEDED, lambda a: artifact_superseded_payload(a)),
    )
    for a in artifacts_:
        if a.id not in roots:
            continue
        for field, event, build in builders:
            reg = artifact_regs.get((a.id, field))
            if reg is not None:
                yield event, _replay_register(build(a), reg)
            elif field == "code_refs" and a.code_refs:
                # Pre-register data: replayed at the artifact's creation time,
                # so any registered (later) write on the peer still wins.
                yield event, _replay(build(a), event, f"{a.id}:{field}", a.created_at)
            elif field == "superseded_by" and a.superseded_at is not None:
                yield event, _replay(build(a), event, f"{a.id}:{field}", a.superseded_at)
