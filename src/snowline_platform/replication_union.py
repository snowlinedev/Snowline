"""The cold-start union merge behind `snowline replicate union`
(replication-continuity §7.1, issue #98).

§7's seed covers the SUPERSET case (a spoke born from the primary's snapshot).
A union covers the REUNION case: two instances that each accumulated history
while no subscription existed between them — e.g. the mini went down, `roam`
was stood up EMPTY and used for days, then the mini came back with months of
history. Neither store is a superset; `pair` alone only replicates forward in
time; a seed's restore would overwrite one side. The union rule is absolute:
**no write authored on either side is absent after the reunion.**

The shape is BACKFILL: each participant's enumerator replays its domain rows as
synthetic events on a reserved `<live source_id>#backfill` stream (fresh epoch,
empty vocabulary, `peer_seen` 0), drained by the ORDINARY delivery loop through
the peer's EXISTING ingest path — the same idempotent apply, origin
suppression, per-name LWW, §6.1 sibling flagging and §8.1 parking. The
convergence rules already exist; the union only supplies the events that were
never emitted.

The four-step procedure (one verb, §5 admin surface only — like `pair`, this is
a pure HTTP client that reaches into no database):

  1. **Pair live streams first.** A fresh pair when none exists; with
     `repair=True`, retire the old streams ON BOTH HALVES OF BOTH SIDES first
     (each instance's outbound subscriptions AND inbound registrations — §7
     step 5's seed helper retires only the primary's halves because a restore
     wipes the spoke's; a union has no restore, and a spoke outbound left
     active would dead-letter its whole old backlog into a retired inbound).
     The re-seed preconditions do NOT apply: the union replaces the restore.
  2. **Backfill the scope namespace, both directions, to completion** —
     `applied_seq` at the walker's final seq and nothing parked — before any
     plugin backfill starts (turns a plugin-before-scope park-and-retry storm
     into a quiet pass).
  3. **Backfill each opted-in plugin, both directions.** An empty side simply
     enumerates nothing.
  4. **Verify, then retire** every backfill stream on both halves. Retired
     streams stay for audit; steady state is one live stream per direction.

Re-running is RESUMABLE: a backfill already open on both halves is waited on,
not re-opened (the receiver's single-active-stream invariant applied to the
backfill id also guarantees one backfill per direction at a time). A completed
union re-run under fresh epochs changes nothing (apply idempotence).

MUTUAL EXCLUSION with the §7 seed: a seed re-prime retires ANY active outbound
targeting the spoke's ingest URL — a backfill outbound included, since it
targets the same URL. Never run `replicate seed` while a union is in flight.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from snowline_plugin_sdk.replication.envelope import backfill_source_id

from snowline_platform import replication_pairing as pairing
from snowline_platform.replication_pairing import (
    PLATFORM_PARTICIPANT,
    PairingError,
    Participant,
    _raise_for_status,
    mint_epoch,
)

Report = Callable[[str], None]

log = logging.getLogger(__name__)


class UnionError(PairingError):
    """A union step failed or stopped for the operator (a parked backfill
    event, a rejected delivery, a timeout, a half-paired pair). A
    `PairingError` subclass so the CLI's error handling covers it unchanged."""


@dataclass
class BackfillStream:
    """One directed backfill stream sender→receiver for one participant — a
    redacted record (never the secret)."""

    participant: str
    sender: Participant
    receiver: Participant
    epoch: str
    subscription_id: str
    final_seq: int
    resumed: bool = False

    @property
    def source_id(self) -> str:
        return backfill_source_id(self.sender.source_id)

    def label(self) -> str:
        return (
            f"{self.participant} {self.sender.source_id} -> "
            f"{self.receiver.source_id} (epoch {self.epoch})"
        )


@dataclass
class UnionResult:
    paired: str = ""  # "paired" | "reused" | "repaired"
    streams: list[BackfillStream] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    old_parks: list[dict] = field(default_factory=list)
    dropped_parks: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --- step 1: live pairing -------------------------------------------------------


def _get_json(client, url: str):
    resp = client.get(url)
    _raise_for_status(resp, f"GET {url}")
    return resp.json()


def _post_json(client, url: str, body: dict):
    resp = client.post(url, json=body)
    _raise_for_status(resp, f"POST {url}")
    return resp.json()


def _live_outbound(client, me: Participant, other: Participant) -> list[dict]:
    """`me`'s ACTIVE live outbound subscriptions toward `other` — keyed by the
    stream identity (`source_id` = mine, `peer_source_id` = the other side's),
    never a backfill stream (shared with `refresh-vocabulary`)."""
    return pairing.live_outbound(client, me, other)


def _live_inbound(client, me: Participant, other: Participant) -> list[dict]:
    """`me`'s ACTIVE inbound registrations from `other`'s live stream."""
    return [
        s
        for s in _get_json(client, f"{me.admin_base}/inbound")
        if s.get("active") and s.get("source_id") == other.source_id
    ]


def live_pairing_state(client, l: Participant, p: Participant) -> str:
    """`paired` (all four halves active), `unpaired` (none), or `partial`."""
    halves = [
        bool(_live_outbound(client, l, p)),
        bool(_live_inbound(client, p, l)),
        bool(_live_outbound(client, p, l)),
        bool(_live_inbound(client, l, p)),
    ]
    if all(halves):
        return "paired"
    if not any(halves):
        return "unpaired"
    return "partial"


def retire_live_streams(
    client, l: Participant, p: Participant, *, report: Report
) -> int:
    """Retire every live stream between `l` and `p` on BOTH halves of BOTH
    sides — each instance's outbound subscriptions AND inbound registrations
    (§7.1 step 1; unlike §7 step 5's primary-only seed helper). Idempotent."""
    n = 0
    for me, other in ((l, p), (p, l)):
        for sub in _live_outbound(client, me, other):
            _post_json(client, f"{me.admin_base}/outbound/retire", {"id": sub["id"]})
            report(f"[union/pair] retired {me.source_id} outbound {sub['id']} "
                   f"(epoch {sub.get('epoch')})")
            n += 1
        for stream in _live_inbound(client, me, other):
            _post_json(
                client,
                f"{me.admin_base}/inbound/retire",
                {"source_id": stream["source_id"], "epoch": stream["epoch"]},
            )
            report(f"[union/pair] retired {me.source_id} inbound from "
                   f"{stream['source_id']} (epoch {stream.get('epoch')})")
            n += 1
    return n


def pair_first(
    client,
    local: dict[str, Participant],
    peer: dict[str, Participant],
    names: list[str],
    *,
    repair: bool,
    report: Report,
) -> str:
    """§7.1 step 1. Returns `paired` (fresh pair), `reused` (every participant
    already live-paired both ways), or `repaired` (old streams retired on both
    halves of both sides, then a fresh pair)."""
    states = {n: live_pairing_state(client, local[n], peer[n]) for n in names}
    if repair:
        for n in names:
            retire_live_streams(client, local[n], peer[n], report=report)
        verdict = "repaired"
    elif all(s == "paired" for s in states.values()):
        report("[union/pair] every participant already live-paired both ways — reusing")
        return "reused"
    elif all(s == "unpaired" for s in states.values()):
        verdict = "paired"
    else:
        odd = sorted(n for n, s in states.items() if s != "unpaired")
        raise UnionError(
            f"live pairing is incomplete or mixed for {odd} "
            f"(states {states}) — re-run with --repair to retire every old "
            f"stream on both halves of both sides and pair fresh (§7.1 step 1)"
        )
    pairing.pair(
        client,
        {n: local[n] for n in names},
        {n: peer[n] for n in names},
        report=report,
    )
    return verdict


# --- steps 2-3: backfill open / progress -----------------------------------------


def _find_backfill_sub(client, sender: Participant, epoch: str) -> dict | None:
    for sub in _get_json(client, f"{sender.admin_base}/backfill"):
        if (
            sub.get("active")
            and sub.get("source_id") == backfill_source_id(sender.source_id)
            and sub.get("epoch") == epoch
        ):
            return sub
    return None


def open_backfill(
    client,
    name: str,
    sender: Participant,
    receiver: Participant,
    *,
    report: Report,
) -> BackfillStream | None:
    """Open ONE directed backfill stream via a §5-SHAPED handshake: the
    RECEIVER registers `(<sender live id>#backfill, fresh epoch)` and mints the
    secret; the SENDER opens the backfill subscription with it and walks its
    enumerator. RESUMES an already-open stream (both halves active) instead of
    opening another. Returns None when the sender has no enumerator (501) —
    the receiver's just-registered half is retired again and the caller warns.
    """
    bf_source = backfill_source_id(sender.source_id)
    open_inbound = [
        s
        for s in _get_json(client, f"{receiver.admin_base}/inbound")
        if s.get("active") and s.get("source_id") == bf_source
    ]
    if open_inbound:
        epoch = open_inbound[0]["epoch"]
        sub = _find_backfill_sub(client, sender, epoch)
        if sub is None:
            raise UnionError(
                f"{receiver.source_id} holds an active backfill inbound from "
                f"{bf_source} (epoch {epoch}) but {sender.source_id} has no "
                f"matching backfill subscription — a half-open backfill. Retire "
                f"the receiver's inbound (POST {receiver.admin_base}/inbound/"
                f"retire) and re-run."
            )
        report(f"[union/backfill] {name}: resuming {bf_source} -> "
               f"{receiver.source_id} (epoch {epoch}, final seq {sub['final_seq']})")
        return BackfillStream(
            name, sender, receiver, epoch, sub["id"], sub["final_seq"], resumed=True
        )

    epoch = mint_epoch()
    reg = client.post(
        f"{receiver.admin_base}/inbound",
        json={"source_id": bf_source, "epoch": epoch},
    )
    _raise_for_status(reg, f"POST {receiver.admin_base}/inbound")
    secret = reg.json()["secret"]
    resp = client.post(
        f"{sender.admin_base}/backfill/open",
        json={
            "target_url": receiver.ingest_url,
            "secret": secret,
            "epoch": epoch,
            "source_id": sender.source_id,
        },
    )
    if resp.status_code >= 400:
        # Undo the receiver's half FIRST on any HTTP failure — a dangling
        # `#backfill` inbound would stop the next run on "half-open backfill"
        # (review of #98). 501 = no enumerator, 404 = a participant predating
        # the backfill surface: both are "skip, loudly" for the caller; any
        # other status (a 409 from a concurrent open, an enumerator raising on
        # a bad row) is an error. A TRANSPORT failure (connection drop, read
        # timeout on a long walk) is deliberately NOT undone here: the sender
        # may have committed the open after the client gave up, and the next
        # run resumes it or stops on the half-open message with instructions.
        try:
            _post_json(
                client,
                f"{receiver.admin_base}/inbound/retire",
                {"source_id": bf_source, "epoch": epoch},
            )
        except Exception as retire_exc:  # noqa: BLE001 — never mask the open failure
            log.warning(
                "backfill open failed (%s) and retiring the receiver's inbound "
                "%s also failed: %s", resp.status_code, bf_source, retire_exc,
            )
        if resp.status_code in (404, 501):
            return None
        _raise_for_status(resp, f"POST {sender.admin_base}/backfill/open")
    opened = resp.json()
    report(
        f"[union/backfill] {name}: opened {bf_source} -> {receiver.source_id} "
        f"(epoch {epoch}): {opened['final_seq']} event(s) {opened.get('enumerated', {})}"
    )
    return BackfillStream(
        name, sender, receiver, epoch, opened["id"], opened["final_seq"]
    )


def progress(client, stream: BackfillStream) -> dict:
    """One stream's completion state, read from BOTH halves: the receiver's
    `applied_seq` + parked events on this stream, the sender's rejected count."""
    inbound = next(
        (
            s
            for s in _get_json(client, f"{stream.receiver.admin_base}/inbound")
            if s.get("source_id") == stream.source_id and s.get("epoch") == stream.epoch
        ),
        None,
    )
    if inbound is None:
        raise UnionError(f"{stream.label()}: the receiver lost its inbound registration")
    parked = [
        p
        for p in _get_json(client, f"{stream.receiver.admin_base}/parked")
        if p.get("source_id") == stream.source_id and p.get("epoch") == stream.epoch
    ]
    sub = next(
        (
            s
            for s in _get_json(client, f"{stream.sender.admin_base}/backfill")
            if s.get("id") == stream.subscription_id
        ),
        {},
    )
    applied = inbound.get("applied_seq", 0)
    return {
        "applied_seq": applied,
        "gate_seq": inbound.get("gate_seq", 0),
        "final_seq": stream.final_seq,
        "parked": parked,
        "rejected": sub.get("rejected", 0),
        "head_error": sub.get("head_error"),
        "complete": applied >= stream.final_seq and not parked,
    }


def wait_for_completion(
    client,
    streams: list[BackfillStream],
    *,
    drive: Callable[[], None],
    timeout: float,
    report: Report,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait until every stream is complete (`applied_seq` == final seq, nothing
    parked). The delivery loops on both instances do the draining; `drive` is
    what happens between polls (production: sleep one poll interval; tests:
    run a delivery pass). Stops LOUDLY — for the operator, never skipping — on
    a parked backfill event (resolve it with re-apply or drop, then re-run:
    the union resumes), a dead-lettered delivery, or the timeout."""
    deadline = clock() + timeout
    while True:
        states = {s.label(): (s, progress(client, s)) for s in streams}
        parked = [(s, st) for s, st in states.values() if st["parked"]]
        if parked:
            lines = "; ".join(
                f"{s.label()}: seq {p['seq']} {p['event_type']} — {p['reason']}"
                for s, st in parked
                for p in st["parked"]
            )
            raise UnionError(
                f"backfill events PARKED (§8.1) — the union stops here: {lines}. "
                f"Fix the cause and re-apply (POST <admin>/parked/reapply), or "
                f"drop it if it is resolved another way (POST <admin>/parked/"
                f"drop), then re-run `snowline replicate union` — it resumes."
            )
        rejected = [(s, st) for s, st in states.values() if st["rejected"]]
        if rejected:
            raise UnionError(
                "backfill deliveries DEAD-LETTERED (§3.1): "
                + "; ".join(f"{s.label()}: {st['head_error']}" for s, st in rejected)
            )
        if all(st["complete"] for _, st in states.values()):
            for s, st in states.values():
                report(f"[union/verify] {s.label()}: applied {st['applied_seq']}/"
                       f"{st['final_seq']}, nothing parked")
            return
        if clock() >= deadline:
            raise UnionError(
                "timed out waiting for backfill completion: "
                + "; ".join(
                    f"{s.label()}: applied {st['applied_seq']}/{st['final_seq']}"
                    + (f" (head error: {st['head_error']})" if st["head_error"] else "")
                    for s, st in states.values()
                    if not st["complete"]
                )
                + " — the streams stay open; re-run to resume waiting"
            )
        drive()


def retire_backfill(client, stream: BackfillStream, *, report: Report) -> None:
    """Retire one backfill stream on BOTH halves (§7.1 step 4)."""
    _post_json(
        client,
        f"{stream.sender.admin_base}/backfill/retire",
        {"id": stream.subscription_id},
    )
    _post_json(
        client,
        f"{stream.receiver.admin_base}/inbound/retire",
        {"source_id": stream.source_id, "epoch": stream.epoch},
    )
    report(f"[union/retire] {stream.label()}: retired on both halves")


def _backfill_both_ways(
    client,
    names: list[str],
    local: dict[str, Participant],
    peer: dict[str, Participant],
    result: UnionResult,
    *,
    report: Report,
) -> list[BackfillStream]:
    opened: list[BackfillStream] = []
    for name in names:
        for sender, receiver in ((local[name], peer[name]), (peer[name], local[name])):
            stream = open_backfill(client, name, sender, receiver, report=report)
            if stream is None:
                msg = (
                    f"WARN {name!r} on {sender.source_id} has no backfill "
                    f"enumerator — its unpaired history does NOT reach "
                    f"{receiver.source_id} (spec §7.1: every participant needs one)"
                )
                result.skipped.append(f"{name}:{sender.source_id}")
                result.notes.append(msg)
                report(msg)
                continue
            opened.append(stream)
    return opened


def check_backfill_surface(
    client, local: dict[str, Participant], peer: dict[str, Participant], names: list[str]
) -> None:
    """Refuse the union unless EVERY participant on BOTH sides serves the §7.1
    backfill surface (`GET <admin>/backfill`). A pre-#98 receiver registers a
    backfill inbound fine (the generic `/inbound` route predates the union) but
    its scope apply still PARKS every shared slug (the old id-keyed collision
    rule) — one park per shared scope, manual cleanup, and the reverse
    direction silently skipped. Probing both sides up front closes the whole
    mixed-version class with one message: upgrade the peer first."""
    missing: list[str] = []
    for name in names:
        for side, inst in (("local", local[name]), ("peer", peer[name])):
            resp = client.get(f"{inst.admin_base}/backfill")
            if resp.status_code in (404, 501):
                missing.append(f"{name}@{inst.source_id} ({side})")
                continue
            _raise_for_status(resp, f"GET {inst.admin_base}/backfill")
    if missing:
        raise UnionError(
            "refusing the union: the §7.1 backfill surface is missing on "
            + ", ".join(missing)
            + " — upgrade the peer first (a pre-#98 instance would park every "
            "shared scope instead of converging it)"
        )


# --- the whole union ----------------------------------------------------------------


def _sleeper(seconds: float) -> Callable[[], None]:
    return lambda: time.sleep(seconds)


def union(
    client,
    local: dict[str, Participant],
    peer: dict[str, Participant],
    *,
    repair: bool = False,
    drive: Callable[[], None] | None = None,
    poll: float = 15.0,
    timeout: float = 3600.0,
    drop_retired_parks: bool = False,
    report: Report = print,
) -> UnionResult:
    """Run the §7.1 four-step union between two discovered instances (see the
    module docstring). Returns what happened, redacted."""
    drive = drive or _sleeper(poll)
    result = UnionResult()
    plan = pairing.plan_pairing(local, peer)
    for note in plan.notes:
        report(note)
    if plan.refused:
        raise UnionError(
            "refusing the union: contract_version mismatch for "
            + ", ".join(repr(n) for n in plan.refused)
        )
    names = plan.to_pair
    check_backfill_surface(client, local, peer, names)

    # 1. live streams first — forward-in-time replication starts NOW.
    result.paired = pair_first(client, local, peer, names, repair=repair, report=report)
    if result.paired != "reused":
        # One poll interval before the first walk: the walker's snapshot is
        # serialized when its open call starts, so a domain transaction that
        # read the subscription table BEFORE the pairing commit (no live outbox
        # row) and commits AFTER that snapshot would be in neither the live
        # stream nor the enumeration. The window is the gap between these two
        # instants; waiting out the longest plausible open transaction closes
        # it in practice (spec §7.1 implementation notes).
        drive()

    # 2. the scope namespace, both directions, to completion.
    scope_names = [n for n in names if n == PLATFORM_PARTICIPANT]
    plugin_names = [n for n in names if n != PLATFORM_PARTICIPANT]
    if scope_names:
        scope_streams = _backfill_both_ways(
            client, scope_names, local, peer, result, report=report
        )
        wait_for_completion(client, scope_streams, drive=drive, timeout=timeout, report=report)
        result.streams.extend(scope_streams)
    else:
        result.notes.append(
            "WARN the platform scope stream is not paired on both instances — "
            "plugin backfills will lean on §8's retryable unknown-scope path"
        )

    # 3. every opted-in plugin, both directions (order among plugins is free).
    plugin_streams = _backfill_both_ways(
        client, plugin_names, local, peer, result, report=report
    )
    wait_for_completion(client, plugin_streams, drive=drive, timeout=timeout, report=report)
    result.streams.extend(plugin_streams)

    # 4. verify (re-read every stream once more), then retire both halves.
    wait_for_completion(client, result.streams, drive=drive, timeout=timeout, report=report)
    for stream in result.streams:
        retire_backfill(client, stream, report=report)

    # Old-epoch leftovers: parks on RETIRED streams are superseded by the
    # backfill (it re-enumerated every row those deliveries carried).
    for inst in _instances(local, peer, names):
        inbound = _get_json(client, f"{inst.admin_base}/inbound")
        retired = {(s["source_id"], s["epoch"]) for s in inbound if not s.get("active")}
        for p in _get_json(client, f"{inst.admin_base}/parked"):
            if (p["source_id"], p["epoch"]) not in retired:
                continue
            entry = {"admin_base": inst.admin_base, **{k: p[k] for k in (
                "source_id", "epoch", "seq", "event_type", "reason")}}
            if drop_retired_parks:
                _post_json(
                    client,
                    f"{inst.admin_base}/parked/drop",
                    {"source_id": p["source_id"], "epoch": p["epoch"], "seq": p["seq"]},
                )
                result.dropped_parks.append(entry)
                report(f"[union/parks] dropped superseded park {p['source_id']}/"
                       f"{p['epoch']} seq {p['seq']} on {inst.admin_base}")
            else:
                result.old_parks.append(entry)
    if result.old_parks:
        report(
            f"NOTE {len(result.old_parks)} parked event(s) remain on RETIRED "
            "pre-union streams — superseded by the backfill; re-run with "
            "--drop-retired-parks to drop them"
        )
    report(
        "union complete — now triage the §6.1 unreconciled view ON BOTH "
        "INSTANCES (a mid-union live write can flag on one side only); "
        "`mark-compatible` is the bulk affordance"
    )
    return result


def _instances(local, peer, names) -> list[Participant]:
    return [side[n] for n in names for side in (local, peer)]
