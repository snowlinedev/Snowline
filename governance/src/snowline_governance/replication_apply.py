"""Governance's replication APPLY — the domain half of the SDK ingest seam
(replication-continuity §4 / §6 / §6.1, #79).

`build_apply` returns the `apply(session, envelope)` the SDK's
`ingest_delivery` runs inside the ingest transaction under origin suppression.
Three rules govern everything here:

**Direct writes, authoring identity preserved.** Apply reconstructs rows from
the payload — authoring UUIDs and timestamps included — via the MODELS, never
the emitting services (which mint fresh ids and re-emit). That makes the two
stores converge on the rows themselves and makes origin suppression structural
on top of the SDK's ContextVar. Idempotence is by primary key: a redelivered or
re-applied event finds its row and no-ops (§4 checklist item 4). The one
deliberate exception to byte-convergence is the conversation `seq` — a local
presentation/resume cursor, re-allocated here under the same branch row lock
the local appender uses (the message SET converges; concurrent interleavings
are presentation-local). A replicated append also BYPASSES the local
archived-branch guard: an event authored before its author saw the archive is
applied, never skipped.

**Every failure raises.** An unknown scope slug, a missing FK target, a scope-
service outage — all raise out of apply, which the SDK classifies as the §8.1
BOUNDED RETRYABLE class: 503 to the sender, redelivery under backoff, a loud
park at the bound. Nothing here swallows an error into a silent skip; that is
the §6.1 "outage is never a silent skip of detection" contract, and it is why
a same-name branch or colliding slug fails LOUD (the §8 posture) instead of
half-applying.

**Same-object races resolve as a pure function (§6).** The in-place mutations
(narrative notes, maturity, governs, a node's graduated-decision pointer) are
last-writer-wins by the event's authoring timestamp with `source_id` as the
stable tiebreak, computed against the `LwwRegister` coordinate the local write
(or prior winning apply) recorded — the same comparison on both sides, so
arrival order can't fork the outcome. A LOSING event is applied-then-
overridden, never skipped: its append half (the rows it or its stream
predecessors created — decision B exists, the graduated decision keeps its
provenance stamps) always lands; only its mutation of the contested row yields.
Every resolved conflict logs at WARNING with BOTH event ids. Decisions
themselves need no LWW: supersession is a branching DAG here (the child's
`supersedes_id`), so "X superseded on both sides" converges mechanically as two
leaves — which is exactly the pair §6.1 then flags.

**§6.1 detection at ingest.** Concurrency is read off the envelope's causal
context: `peer_seen` is the contiguous applied frontier of MY stream the author
had applied when authoring, so every locally-authored decision whose outbox
`seq` (on my stream toward that author) is GREATER than `peer_seen` is
concurrent with the incoming event — exact, clock-free. The collision surface
is the APPLICABILITY CHAIN, not same-scope: the pair collides when either
decision's scope lies on the other's ancestors-until-isolated walk (the
platform scope service's `/ancestors`, resolved live at apply time — its
outage is the bounded retryable error above). Sibling scopes that merely share
an ancestor do NOT collide (the §10 non-inheriting negative case). Detection
runs symmetrically on both instances and writes the same normalized
`DecisionConcurrence` pair; the `unreconciled` view derives the open flags.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from snowline_plugin_sdk.replication.models import (
    ReplicationOutboxRow,
    ReplicationSubscription,
)
from sqlalchemy import func, select

from snowline_governance import concurrence, replication_stream
from snowline_governance.contract import (
    EVENT_ARTIFACT_GOVERNS_SET,
    EVENT_ARTIFACT_MATURITY_SET,
    EVENT_ARTIFACT_REGISTERED,
    EVENT_ARTIFACT_RESOLVED,
    EVENT_ARTIFACT_REVISED,
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
from snowline_governance.models import (
    SHADOW_BRANCH_STATUS_ARCHIVED,
    Artifact,
    ArtifactGoverns,
    ArtifactVersion,
    Decision,
    LwwRegister,
    ShadowBranch,
    ShadowConversationEvent,
    ShadowNode,
    ShadowNodeCitation,
)
from snowline_governance.scope_client import HttpScopeClient, ScopeClient

log = logging.getLogger("snowline.governance.replication_apply")

_DECISION_EVENTS = (EVENT_DECISION_RECORDED, EVENT_DECISION_SUPERSEDED)


def build_apply(scope_client: ScopeClient | None = None):
    """The plugin's apply function for `ingest_delivery` /
    `build_replication_router`. `scope_client` is injectable (tests pass a
    stub); production defaults to the real `HttpScopeClient` — co-located on
    loopback (§5.1), so the §6.1 round-trip shares fate with the instance, not
    the tailnet."""
    client: ScopeClient = scope_client or HttpScopeClient()

    def apply(session, envelope: dict) -> None:
        handler = _HANDLERS.get(envelope.get("event_type"))
        if handler is None:
            # Unknown vocabulary (a newer peer's event type) — retryable, so it
            # holds under backoff and PARKS loudly rather than mis-applying.
            raise ValueError(
                f"unknown replicated event type {envelope.get('event_type')!r}"
            )
        handler(session, client, envelope)

    return apply


# --- small shared helpers -----------------------------------------------------


def _uuid(value) -> uuid.UUID | None:
    return None if value is None else uuid.UUID(str(value))


def _dt(value) -> datetime | None:
    return None if value is None else replication_stream.parse_at(value)


def _require_scope(client: ScopeClient, slug: str) -> None:
    """§8: an unknown scope slug is a RETRYABLE apply error — ordinary
    scope-stream lag self-heals on redelivery; a slug that never materializes
    parks at the bound. `ScopeServiceError` (outage) propagates by itself."""
    if client.resolve(slug) is None:
        raise ValueError(
            f"unknown scope slug {slug!r} — not (yet) present on this instance"
        )


def _require(row, what: str):
    """A referenced row that should precede this event on its stream (or be a
    local write). Missing = it hasn't applied here (e.g. parked ahead) —
    retryable, never a silent skip."""
    if row is None:
        raise ValueError(f"replicated event references a missing {what}")
    return row


def _unseen_local_rows(session, envelope: dict) -> list[ReplicationOutboxRow]:
    """The §3.2 concurrency read: MY outbox rows on the stream toward the
    envelope's author with `seq > peer_seen` — exactly the locally-authored
    events the author had NOT applied when it authored this event (contiguous
    apply makes the comparison exact). Empty when no outbound stream toward the
    author exists (one-way pairing: nothing computable).

    OUTBOX RETENTION IS LOAD-BEARING: this deliberately reads DELIVERED rows
    too — a row's seq stays comparable to `peer_seen` long after delivery
    (delivered ≠ applied-and-acknowledged-in-a-later-authored-event). Any
    future outbox-pruning job that drops delivered rows above a peer's lowest
    in-flight `peer_seen` would silently blind §6.1 sibling detection and the
    §6 concurrent-override warning."""
    sub = session.scalars(
        select(ReplicationSubscription)
        .where(
            ReplicationSubscription.active.is_(True),
            ReplicationSubscription.peer_source_id == envelope.get("source"),
        )
        .order_by(ReplicationSubscription.created_at.desc())
    ).first()
    if sub is None:
        return []
    return list(
        session.scalars(
            select(ReplicationOutboxRow)
            .where(
                ReplicationOutboxRow.subscription_id == sub.id,
                ReplicationOutboxRow.seq > (envelope.get("peer_seen") or 0),
            )
            .order_by(ReplicationOutboxRow.seq)
        )
    )


# --- §6 LWW (same-object conflict apply) ---------------------------------------


def _lww_apply(
    session,
    envelope: dict,
    kind: str,
    object_id: uuid.UUID,
    field: str,
    setter,
) -> bool:
    """Apply one register-class mutation under LWW-by-event-timestamp with
    `source_id` tiebreak (§6). Pure function of the two coordinates — the
    incoming event's `(at, source)` vs the register's — so both sides pick the
    same winner regardless of arrival order. Ties (same coordinate = a replay
    of the current write) apply idempotently without conflict noise.

    Returns True when the incoming event won (setter ran, register advanced);
    False when it LOST — the mutation yields to the newer value, logged at
    WARNING with both event ids."""
    payload = envelope["payload"]
    at = replication_stream.parse_at(payload["at"])
    source = envelope["source"]
    event_id = payload["event_id"]
    register = session.get(LwwRegister, (kind, object_id, field))
    if register is not None and (at, source) < (
        register.written_at,
        register.source_id,
    ):
        log.warning(
            "replication conflict resolved by LWW on %s %s.%s: incoming event "
            "%s (source %s, at %s) LOST to event %s (source %s, at %s) — "
            "overridden, not skipped (§6)",
            kind, object_id, field,
            event_id, source, at.isoformat(),
            register.event_ref, register.source_id,
            register.written_at.isoformat(),
        )
        return False
    # A genuine concurrent override (not a replay, not a sequential follow-up):
    # the register's current write is one the AUTHOR had not applied when it
    # authored this event — the same peer_seen read §6.1 uses.
    concurrent_override = (
        register is not None
        and register.event_ref != event_id
        and any(
            ((row.payload or {}).get("payload") or {}).get("event_id")
            == register.event_ref
            for row in _unseen_local_rows(session, envelope)
        )
    )
    prior_ref = register.event_ref if register is not None else None
    setter()
    replication_stream.record_register(
        session, kind, object_id, field, at=at, source=source, event_ref=event_id
    )
    if concurrent_override:
        log.warning(
            "replication conflict resolved by LWW on %s %s.%s: incoming event "
            "%s (source %s) WON over concurrent event %s (§6)",
            kind, object_id, field, event_id, source, prior_ref,
        )
    return True


# --- §6.1 concurrent-sibling detection ------------------------------------------


def _detect_concurrent_siblings(session, client: ScopeClient, envelope: dict) -> None:
    """Flag the incoming decision against every concurrent locally-authored
    decision it collides with along the applicability chain (module docstring).
    Idempotent (redelivery / re-apply re-derives the same pairs). Raises on a
    scope-service outage or unknown slug — bounded retryable, never a skip."""
    payload = envelope["payload"]
    candidates = [
        row
        for row in _unseen_local_rows(session, envelope)
        if row.event_type in _DECISION_EVENTS
    ]
    if not candidates:
        return
    incoming_id = _uuid(payload["id"])
    incoming_scope_id = payload["scope_id"]
    # The incoming decision's ancestors-until-isolated chain, by STABLE id
    # (#11). The §8 unknown-slug gate lives in `_apply_decision` (this walk
    # only runs when candidates exist, so it cannot be that gate); a raise
    # here — outage, or a candidate's since-unknown slug — is the same
    # retryable class.
    incoming_chain = {str(sc["id"]) for sc in client.ancestors(payload["scope"])}
    chains: dict[str, set[str]] = {}
    for row in candidates:
        local = (row.payload or {}).get("payload") or {}
        local_id = _uuid(local.get("id"))
        if local_id is None or local_id == incoming_id:
            continue
        # Collision surface: the local decision's scope on the incoming's
        # chain, OR the incoming's scope on the local's chain — parent-scope
        # governance runs BOTH directions; unrelated siblings match neither.
        collides = local.get("scope_id") in incoming_chain
        if not collides:
            slug = local.get("scope")
            if slug not in chains:
                chains[slug] = {str(sc["id"]) for sc in client.ancestors(slug)}
            collides = incoming_scope_id in chains[slug]
        if collides and concurrence.flag_pair(session, incoming_id, local_id):
            log.info(
                "§6.1 concurrent siblings flagged: incoming decision %s "
                "(scope %s) vs local decision %s (scope %s) — see the "
                "unreconciled view",
                incoming_id, payload["scope"], local_id, local.get("scope"),
            )


# --- per-event-type handlers ----------------------------------------------------


def _apply_decision(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    decision_id = _uuid(p["id"])
    if session.get(Decision, decision_id) is None:
        # §8 unknown-slug gate, same as every scope-bearing apply. Detection
        # below only reaches the scope service when concurrent candidates
        # exist, so it cannot double as this gate — without it, a decision in
        # a not-yet-replicated scope would apply ungated in the common
        # (peer_seen-current) case instead of retrying until the scope stream
        # catches up.
        _require_scope(client, p["scope"])
        session.add(
            Decision(
                id=decision_id,
                scope_id=_uuid(p["scope_id"]),
                scope_slug=p["scope"],
                decision=p["decision"],
                rationale=p.get("rationale"),
                recorded_at=_dt(p.get("recorded_at")),
                # A superseded event's append half: the new leaf + its edge.
                # A missing prior is an FK error → retryable → parks loud.
                supersedes_id=_uuid(p.get("supersedes_id")),
            )
        )
        session.flush()
    _detect_concurrent_siblings(session, client, envelope)


def _apply_branch_created(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    branch_id = _uuid(p["id"])
    if session.get(ShadowBranch, branch_id) is not None:
        return
    _require_scope(client, p["scope"])
    session.add(
        ShadowBranch(
            id=branch_id,
            scope_id=_uuid(p["scope_id"]),
            scope_slug=p["scope"],
            name=p["name"],
            narrative_notes=p.get("narrative_notes"),
            created_at=_dt(p.get("created_at")),
        )
    )
    # A same-(scope, name) branch authored on both sides trips the unique
    # constraint here — loud + retryable + parked, the §8 collision posture.
    session.flush()


def _apply_branch_archived(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    branch = _require(
        session.get(ShadowBranch, _uuid(p["branch_id"])), "shadow branch"
    )
    incoming_at = _dt(p.get("archived_at")) or replication_stream.parse_at(p["at"])
    if branch.status != SHADOW_BRANCH_STATUS_ARCHIVED:
        branch.status = SHADOW_BRANCH_STATUS_ARCHIVED
        branch.archived_at = incoming_at
    elif branch.archived_at is None or incoming_at < branch.archived_at:
        # Both sides archived across a partition: converge on the EARLIEST
        # archival (min is a pure function of the two events — deterministic
        # on both sides, and it matches archive_branch's "pinned to the
        # original archival" semantics).
        branch.archived_at = incoming_at
    session.flush()


def _apply_notes_set(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    branch_id = _uuid(p["branch_id"])
    branch = _require(session.get(ShadowBranch, branch_id), "shadow branch")
    _lww_apply(
        session,
        envelope,
        "shadow_branch",
        branch_id,
        "narrative_notes",
        lambda: setattr(branch, "narrative_notes", p.get("narrative_notes")),
    )
    session.flush()


def _apply_node_added(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    node_id = _uuid(p["id"])
    if session.get(ShadowNode, node_id) is not None:
        return
    session.add(
        ShadowNode(
            id=node_id,
            branch_id=_uuid(p["branch_id"]),
            statement=p["statement"],
            rationale=p.get("rationale"),
            created_at=_dt(p.get("created_at")),
        )
    )
    session.flush()


def _apply_citation_added(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    citation_id = _uuid(p["id"])
    if session.get(ShadowNodeCitation, citation_id) is not None:
        return
    node_id = _uuid(p["node_id"])
    cited_node_id = _uuid(p.get("cited_node_id"))
    cited_decision_id = _uuid(p.get("cited_decision_id"))
    # Same-EDGE dedupe: the partial unique indexes hold one row per
    # (node, target) pair, so the same citation authored on both sides
    # converges on the edge (each instance keeps its first-applied row id —
    # the id is display-only; the edge is the domain fact).
    target_filter = (
        ShadowNodeCitation.cited_node_id == cited_node_id
        if cited_node_id is not None
        else ShadowNodeCitation.cited_decision_id == cited_decision_id
    )
    existing = session.scalar(
        select(ShadowNodeCitation.id).where(
            ShadowNodeCitation.node_id == node_id, target_filter
        )
    )
    if existing is not None:
        return
    session.add(
        ShadowNodeCitation(
            id=citation_id,
            node_id=node_id,
            cited_node_id=cited_node_id,
            cited_decision_id=cited_decision_id,
            created_at=_dt(p.get("created_at")),
        )
    )
    session.flush()


def _apply_conversation_appended(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    event_id = _uuid(p["id"])
    if session.get(ShadowConversationEvent, event_id) is not None:
        return
    # The same allocator discipline as shadow's local appender (branch row
    # FOR UPDATE → max(seq)+1) but WITHOUT the archived guard: a replicated
    # append authored before its author saw the archive is applied, never
    # skipped (module docstring; seq is presentation-local).
    branch_id = _uuid(p["branch_id"])
    branch = _require(
        session.scalar(
            select(ShadowBranch)
            .where(ShadowBranch.id == branch_id)
            .with_for_update()
        ),
        "shadow branch",
    )
    next_seq = (
        session.scalar(
            select(func.max(ShadowConversationEvent.seq)).where(
                ShadowConversationEvent.branch_id == branch.id
            )
        )
        or 0
    ) + 1
    session.add(
        ShadowConversationEvent(
            id=event_id,
            branch_id=branch.id,
            seq=next_seq,
            kind=p["kind"],
            payload=p["payload"],
            created_at=_dt(p.get("created_at")),
        )
    )
    session.flush()


def _apply_graduated(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    decision_id = _uuid(p["decision_id"])
    decision = _require(session.get(Decision, decision_id), "decision")
    node_id = _uuid(p.get("node_id"))
    if node_id is None:
        # Branch-level stamp (end decision / rejection): each side stamps the
        # decision IT minted, so there is no shared row to race — idempotent.
        decision.shadow_origin_label = p["label"]
        decision.shadow_origin_kind = p.get("kind")
        session.flush()
        return
    node = _require(session.get(ShadowNode, node_id), "shadow node")
    # The decision's provenance stamps are the APPEND half — they describe this
    # decision only and always land, even when the node pointer loses LWW
    # (applied-then-overridden: the graduated decision survives with its
    # provenance; only the contested pointer yields).
    decision.shadow_origin_node_id = str(node_id)
    decision.shadow_origin_label = p["label"]
    decision.shadow_origin_kind = p.get("kind")
    _lww_apply(
        session,
        envelope,
        "shadow_node",
        node_id,
        "graduated_decision_id",
        lambda: setattr(node, "graduated_decision_id", decision_id),
    )
    session.flush()


def _apply_artifact_registered(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    governs = p.get("governs") or []
    for edge in governs:
        _require_scope(client, edge["scope"])
    artifact_id = _uuid(p["id"])
    if session.get(Artifact, artifact_id) is None:
        session.add(
            Artifact(
                id=artifact_id,
                doc_kind=p["doc_kind"],
                backend=p["backend"],
                maturity=p["maturity"],
                governs_all=bool(p.get("governs_all")),
                created_at=_dt(p.get("created_at")),
            )
        )
        session.flush()
    version = p["version"]
    version_id = _uuid(version["id"])
    if session.get(ArtifactVersion, version_id) is None:
        session.add(
            ArtifactVersion(
                id=version_id,
                artifact_id=artifact_id,
                body_snapshot=version.get("body_snapshot"),
                created_at=_dt(version.get("created_at")),
            )
        )
    for edge in governs:
        scope_id = _uuid(edge["scope_id"])
        if session.get(ArtifactGoverns, (artifact_id, scope_id)) is None:
            session.add(
                ArtifactGoverns(
                    artifact_id=artifact_id,
                    scope_id=scope_id,
                    scope_slug=edge["scope"],
                )
            )
    session.flush()


def _apply_artifact_revised(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    version = p["version"]
    version_id = _uuid(version["id"])
    if session.get(ArtifactVersion, version_id) is not None:
        return
    session.add(
        ArtifactVersion(
            id=version_id,
            artifact_id=_uuid(p["artifact_id"]),
            supersedes_id=_uuid(version.get("supersedes_id")),
            relation=version.get("relation"),
            body_snapshot=version.get("body_snapshot"),
            summary=version.get("summary"),
            created_at=_dt(version.get("created_at")),
        )
    )
    session.flush()


def _apply_artifact_resolved(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    version = _require(
        session.get(ArtifactVersion, _uuid(p["version_id"])), "artifact version"
    )
    # A one-way, idempotent flip: two sides resolving DIFFERENT losers both
    # converge by applying both flips (no LWW register needed).
    version.status = "superseded"
    session.flush()


def _apply_maturity_set(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    artifact_id = _uuid(p["artifact_id"])
    artifact = _require(session.get(Artifact, artifact_id), "artifact")
    _lww_apply(
        session,
        envelope,
        "artifact",
        artifact_id,
        "maturity",
        lambda: setattr(artifact, "maturity", p["maturity"]),
    )
    session.flush()


def _apply_governs_set(session, client, envelope: dict) -> None:
    p = envelope["payload"]
    artifact_id = _uuid(p["artifact_id"])
    artifact = _require(session.get(Artifact, artifact_id), "artifact")

    def replace_governs() -> None:
        # The write is a wholesale replace, so the whole set is the register's
        # value. Scope existence is checked only on the WINNING path (a losing
        # event applies nothing, so lag on ITS slugs must not stall the stream).
        for edge in p.get("governs") or []:
            _require_scope(client, edge["scope"])
        session.execute(
            ArtifactGoverns.__table__.delete().where(
                ArtifactGoverns.artifact_id == artifact_id
            )
        )
        artifact.governs_all = bool(p.get("governs_all"))
        for edge in p.get("governs") or []:
            session.add(
                ArtifactGoverns(
                    artifact_id=artifact_id,
                    scope_id=_uuid(edge["scope_id"]),
                    scope_slug=edge["scope"],
                )
            )

    _lww_apply(
        session, envelope, "artifact", artifact_id, "governs", replace_governs
    )
    session.flush()


_HANDLERS = {
    EVENT_DECISION_RECORDED: _apply_decision,
    EVENT_DECISION_SUPERSEDED: _apply_decision,
    EVENT_SHADOW_BRANCH_CREATED: _apply_branch_created,
    EVENT_SHADOW_BRANCH_ARCHIVED: _apply_branch_archived,
    EVENT_SHADOW_NOTES_SET: _apply_notes_set,
    EVENT_SHADOW_NODE_ADDED: _apply_node_added,
    EVENT_SHADOW_CITATION_ADDED: _apply_citation_added,
    EVENT_SHADOW_CONVERSATION_APPENDED: _apply_conversation_appended,
    EVENT_SHADOW_GRADUATED: _apply_graduated,
    EVENT_ARTIFACT_REGISTERED: _apply_artifact_registered,
    EVENT_ARTIFACT_REVISED: _apply_artifact_revised,
    EVENT_ARTIFACT_RESOLVED: _apply_artifact_resolved,
    EVENT_ARTIFACT_MATURITY_SET: _apply_maturity_set,
    EVENT_ARTIFACT_GOVERNS_SET: _apply_governs_set,
}
