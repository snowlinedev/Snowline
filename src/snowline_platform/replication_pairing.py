"""The pairing library behind `snowline replicate pair` (replication-continuity
§5, issue #82).

Pairing is a one-per-pair operator step that stands up the replication streams
between two full instances. It is NOT an MCP surface (§5: agents never manage
plumbing) — it drives BOTH instances over the SDK's tailnet-gated
replication-admin surface (`snowline_plugin_sdk.replication.admin`), performing
the per-direction receiver-mints-secret handshake for every plugin that declares
`replication` on both sides, plus the platform's own scope stream (§8, the
platform dogfoods the contract).

This module is a pure HTTP client of that admin surface — it imports no plugin
code and reaches into no database. It talks JSON to `/plugins` (to discover who
replicates) and to each participant's `/replication-admin` routes (to run the
handshake), so it composes the live stack without coupling to any plugin's
internals.

The handshake, per direction sender→receiver (§5):
  1. ask the RECEIVER to register the inbound stream `(sender_source_id, epoch)`
     — the receiver MINTS the epoch's secret, stores it, and returns it once
     over the tailnet (WireGuard-encrypted transport; never logged);
  2. create the SENDER's outbound subscription (receiver `ingest_path` + stream
     + that secret), wiring `peer_source_id` to the REVERSE stream so the
     sender's `peer_seen` reports the applied frontier of what it has received
     (§3.2 causal context).
Run for both directions, the two instances converge by events alone thereafter.

Warnings (§5/§10): a plugin opted into replication on one side only is a
one-sided opt-in — WARNed and skipped (there is no peer to pair it with). A
matched pair whose declared `contract_version`s differ is REFUSED with a clear
message (§10 — a version mismatch is a bug to fix, not a stream to open). A
vocabulary (`events`) mismatch is WARNed but paired: the union still flows, and
an event type one side doesn't know simply never arrives from it.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("snowline_platform.replication_pairing")

# The platform's own replication participation (§8) is not in its `/plugins`
# registry, so — unlike a plugin's — its contract is not discoverable from a
# manifest block there. Instead the platform SELF-DESCRIBES it at a small
# tailnet-gated self-manifest endpoint (issue #95), the same block shape a
# plugin declares, which this CLI reads through the IDENTICAL construction path
# (`_participant`) so the platform's scope stream is version/vocabulary-checked
# at pairing exactly like a plugin's — no synthesized constants, no skipped
# refuse (that was #90's special case, now deleted).
PLATFORM_PARTICIPANT = "platform"
PLATFORM_MANIFEST_PATH = "/replication/manifest"

DEFAULT_ADMIN_PREFIX = "/replication-admin"


class PairingError(RuntimeError):
    """A pairing step failed hard (an admin route errored, or a pair was refused
    for a reason the operator must resolve before re-running)."""


@dataclass(frozen=True)
class Participant:
    """One replicating participant on one instance — a plugin (discovered from
    the platform's `/plugins` registry) or the platform's own scope stream.

    `source_id` is the instance-qualified `<instance>.<name>` the SDK stamps at
    emit (§3); `admin_base`/`ingest_url` are the absolute URLs this participant
    serves the §5 admin surface and its `ingest_path` on. `contract_version`
    comes straight from the participant's declared block — a plugin's manifest
    `replication` block (§4) or the platform's self-manifest (§8/#95). It is
    None only for a participant that declares no version at all (a defensive
    fallback; nothing in a composed stack does today); a None on either side
    skips the refuse and holds any real skew at delivery time (§3.2)."""

    name: str
    admin_base: str
    ingest_url: str
    source_id: str
    events: tuple[str, ...]
    contract_version: int | None


@dataclass
class PairPlan:
    """What `pair` decided to do, before/after driving the admin surface —
    returned so the CLI (and tests) can report and assert on it."""

    to_pair: list[str] = field(default_factory=list)
    one_sided: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    vocab_warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    streams: list[dict] = field(default_factory=list)


def mint_epoch() -> str:
    """A fresh stream epoch (§3.2) — minted at pairing, re-minted at re-pair. An
    opaque, sortable, collision-proof token: a millisecond stamp for eyeball
    ordering plus random bytes so two epochs minted in the same tick can never
    collide."""
    return f"{int(time.time() * 1000):x}-{secrets.token_hex(4)}"


# --- discovery ----------------------------------------------------------------


def discover_participants(
    client,
    platform_url: str,
    instance_id: str,
    *,
    admin_prefix: str = DEFAULT_ADMIN_PREFIX,
    include_platform: bool = True,
    reachable_host: str | None = None,
) -> dict[str, Participant]:
    """Every replicating participant on the instance at `platform_url`, keyed by
    name. Plugins come from `GET /plugins` (a plugin replicates iff its manifest
    carries a `replication` block, §4); the platform's scope stream (§8) is read
    from its self-manifest endpoint (issue #95) unless `include_platform=False`.
    BOTH are turned into a Participant the same way (`_participant`), so the
    platform stream is version/vocabulary-checked at pairing exactly like a
    plugin's.

    `client` is any object with `.get(url) -> response` (an `httpx.Client` in
    production). `platform_url` is the instance's platform base.

    CROSS-TAILNET ADDRESSING (§4.1/§5.1): a plugin advertises a LOOPBACK
    `base_url` to its own platform's registry (it binds loopback; the tailnet
    path is tailscaled's). That loopback base_url is directly usable when
    discovering the LOCAL instance, but NOT when discovering a PEER over the
    tailnet. `reachable_host` (the peer's tailnet host) is how a peer's plugin
    gets a reachable address, by the §4.1 advertised-address rule
    (`_resolve_base`): the plugin's declared `advertised_base_url` if present,
    else a port-preserving rewrite of its loopback `base_url` onto
    `reachable_host`. The platform participant is addressed via `platform_url`
    (already the reachable address), so it is never advertised-rewritten."""
    platform_url = platform_url.rstrip("/")
    resp = client.get(f"{platform_url}/plugins")
    _raise_for_status(resp, f"GET {platform_url}/plugins")
    participants: dict[str, Participant] = {}
    for entry in resp.json().get("plugins", []):
        manifest = entry.get("manifest", {})
        block = manifest.get("replication")
        if not block:
            continue  # not opted in — degrades alone (§4)
        name = manifest["name"]
        base = _resolve_base(block, manifest["base_url"], reachable_host)
        participants[name] = _participant(name, base, block, instance_id, admin_prefix)
    if include_platform:
        block = _platform_manifest(client, platform_url)
        # The platform is discovered AT platform_url (already the reachable
        # address), so its base is platform_url verbatim — the §4.1 rewrite is a
        # plugin concern (§8).
        participants[PLATFORM_PARTICIPANT] = _participant(
            PLATFORM_PARTICIPANT, platform_url, block, instance_id, admin_prefix
        )
    return participants


def _participant(
    name: str, base: str, block: dict, instance_id: str, admin_prefix: str
) -> Participant:
    """Build a Participant from a `replication`-block-shaped dict — the ONE
    construction path for both a plugin (block from `/plugins`, §4) and the
    platform (block from its self-manifest, §8/#95), so both carry a real
    declared `contract_version`/vocabulary and are refused/warned identically at
    pairing."""
    base = base.rstrip("/")
    return Participant(
        name=name,
        admin_base=f"{base}{admin_prefix}",
        ingest_url=f"{base}{block['ingest_path']}",
        source_id=f"{instance_id}.{name}",
        events=tuple(block.get("events", [])),
        contract_version=block.get("contract_version"),
    )


def _resolve_base(
    block: dict, registry_base_url: str, reachable_host: str | None
) -> str:
    """The §4.1 advertised-address rule for a plugin discovered on an instance.

    LOCAL discovery (`reachable_host` None) uses the registry `base_url` as-is —
    the loopback address is directly reachable. CROSS-TAILNET discovery prefers
    the plugin's declared `advertised_base_url` (the peer-reachable address it
    states for itself), and with none falls back to the port-preserving host
    rewrite. The fallback is BYTE-IDENTICAL to pre-#96 behavior, so a plugin
    that never declares the field pairs exactly as it does today."""
    if reachable_host is None:
        return registry_base_url
    advertised = block.get("advertised_base_url")
    if advertised:
        return advertised
    return _rehost(registry_base_url.rstrip("/"), reachable_host)


def _platform_manifest(client, platform_url: str) -> dict:
    """The platform's replication self-manifest (§8, issue #95) — the same block
    shape a plugin declares, read from its tailnet-gated endpoint so the CLI
    builds the platform Participant through the identical `_participant` path."""
    resp = client.get(f"{platform_url}{PLATFORM_MANIFEST_PATH}")
    _raise_for_status(resp, f"GET {platform_url}{PLATFORM_MANIFEST_PATH}")
    return resp.json()


# --- the §5 handshake ---------------------------------------------------------


def handshake_direction(
    client,
    sender: Participant,
    receiver: Participant,
    *,
    epoch: str | None = None,
    report: Callable[[str], None] = lambda _msg: None,
) -> dict:
    """Open ONE directed stream sender→receiver via the §5 receiver-mints
    handshake, and return a redacted record of it (never the secret).

    Step 1: the receiver registers the inbound stream and mints the secret.
    Step 2: the sender creates the outbound subscription carrying that secret,
    with `peer_source_id = receiver.source_id` so the sender's `peer_seen`
    reports the applied frontier of the REVERSE stream it receives from the peer
    (§3.2). The secret lives only in this function's frame and is dropped on
    return."""
    epoch = epoch or mint_epoch()
    # Contract-version refuse (§10), enforced HERE so it covers every caller —
    # `pair` (which also refuses up front in `plan_pairing`) AND the §7 seed's
    # reverse-pair, which reaches the handshake directly. A version mismatch is a
    # bug to fix, not a stream to open.
    refuse_on_version_mismatch(sender, receiver)
    # Run-once guard (§5): the handshake mints a FRESH epoch each call, so the
    # receiver's own duplicate-(source_id, epoch) guard can't catch a re-pair —
    # a second run would silently open a SECOND live stream from the same sender.
    # So refuse if the receiver already holds an ACTIVE inbound stream from this
    # sender, whatever its epoch. Re-pairing a live stream is rotation or a
    # fresh-epoch re-seed (which retires the old stream first), never this.
    existing = client.get(f"{receiver.admin_base}/inbound")
    _raise_for_status(existing, f"GET {receiver.admin_base}/inbound")
    if any(
        s.get("source_id") == sender.source_id and s.get("active")
        for s in existing.json()
    ):
        raise PairingError(
            f"{receiver.name} already holds an active inbound stream from "
            f"{sender.source_id} — this pair looks already paired. Rotate the "
            f"secret or re-seed under a fresh epoch (which retires the old "
            f"stream first) instead of re-pairing."
        )
    report(
        f"  {sender.source_id} -> {receiver.source_id}: registering inbound "
        f"(epoch {epoch}) on receiver"
    )
    reg = client.post(
        f"{receiver.admin_base}/inbound",
        json={"source_id": sender.source_id, "epoch": epoch},
    )
    if reg.status_code == 409:
        raise PairingError(
            f"inbound stream ({sender.source_id}, {epoch}) already exists on "
            f"{receiver.name} — this pair looks already paired. Rotate the "
            f"secret or re-seed under a fresh epoch instead of re-pairing."
        )
    _raise_for_status(reg, f"POST {receiver.admin_base}/inbound")
    secret = reg.json()["secret"]

    report(f"  {sender.source_id} -> {receiver.source_id}: creating outbound on sender")
    out = client.post(
        f"{sender.admin_base}/outbound",
        json={
            "target_url": receiver.ingest_url,
            "secret": secret,
            "event_types": list(sender.events),
            "epoch": epoch,
            "source_id": sender.source_id,
            "peer_source_id": receiver.source_id,
        },
    )
    _raise_for_status(out, f"POST {sender.admin_base}/outbound")
    return {
        "participant": sender.name,
        "source_id": sender.source_id,
        "peer_source_id": receiver.source_id,
        "epoch": epoch,
        "target_url": receiver.ingest_url,
        "event_types": list(sender.events),
        "subscription_id": out.json().get("id"),
    }


# --- the whole pairing run ----------------------------------------------------


def _version_mismatch(a: Participant, b: Participant) -> bool:
    """True iff both sides declare a contract_version and they DIFFER. A None on
    either side (a participant that declares no version at all) is not a
    mismatch — any real skew there holds at delivery time (§3.2). The platform
    USED to be that None case; it now self-declares (§8/#95) and is refused on
    skew exactly like a plugin."""
    return (
        a.contract_version is not None
        and b.contract_version is not None
        and a.contract_version != b.contract_version
    )


def refuse_on_version_mismatch(a: Participant, b: Participant) -> None:
    """Raise `PairingError` if `a` and `b` declare mismatched contract_versions
    (§10 — pairing REFUSES a version-mismatched pair). Shared by `plan_pairing`
    (the up-front `pair` check) and `handshake_direction` (so the seed's
    reverse-pair is guarded too)."""
    if _version_mismatch(a, b):
        raise PairingError(
            f"contract_version mismatch pairing {a.name!r} "
            f"({a.source_id} v{a.contract_version} vs {b.source_id} "
            f"v{b.contract_version}) — upgrade the lagging side's SDK before "
            f"pairing (§3.2/§10)."
        )


def plan_pairing(
    local: dict[str, Participant],
    peer: dict[str, Participant],
) -> PairPlan:
    """Decide, without touching the wire, which participants to pair and which to
    warn/refuse (§5/§10). Pure over the two discovered participant maps, so the
    CLI can print the plan before acting and tests can assert on it."""
    plan = PairPlan()
    for name in sorted(set(local) | set(peer)):
        l, p = local.get(name), peer.get(name)
        if l is None or p is None:
            present = "local" if l is not None else "peer"
            plan.one_sided.append(name)
            plan.notes.append(
                f"WARN one-sided opt-in: {name!r} declares replication on the "
                f"{present} instance only — skipped (no peer to pair it with)."
            )
            continue
        if _version_mismatch(l, p):
            plan.refused.append(name)
            plan.notes.append(
                f"REFUSE {name!r}: contract_version mismatch "
                f"(local {l.contract_version}, peer {p.contract_version}) — "
                f"upgrade the lagging side's SDK before pairing (§3.2)."
            )
            continue
        if set(l.events) != set(p.events):
            plan.vocab_warnings.append(name)
            only_local = sorted(set(l.events) - set(p.events))
            only_peer = sorted(set(p.events) - set(l.events))
            plan.notes.append(
                f"WARN {name!r}: event vocabulary differs (local-only "
                f"{only_local}, peer-only {only_peer}) — pairing anyway; an "
                f"event type one side never emits simply never arrives."
            )
        if l.contract_version is None or p.contract_version is None:
            plan.notes.append(
                f"NOTE {name!r}: contract_version not manifest-declared; a "
                f"version skew here holds at delivery time (§3.2), not here."
            )
        plan.to_pair.append(name)
    return plan


def pair(
    client,
    local: dict[str, Participant],
    peer: dict[str, Participant],
    *,
    report: Callable[[str], None] = print,
) -> PairPlan:
    """Run the full §5 pairing between two discovered instances: plan, then for
    every mutually-opted-in participant open BOTH directed streams
    (local→peer and peer→local) via the receiver-mints handshake. Returns the
    plan, its `streams` filled in with a redacted record per directed stream.

    Idempotency: pairing runs ONCE per pair. A re-run hits the receiver's
    duplicate-registration guard and raises `PairingError` (rotate or re-seed
    instead of re-pairing) rather than silently forking a live stream."""
    plan = plan_pairing(local, peer)
    for note in plan.notes:
        report(note)
    if plan.refused:
        raise PairingError(
            "refusing to pair "
            + ", ".join(repr(n) for n in plan.refused)
            + " on contract_version mismatch (see warnings above); resolve and "
            "re-run"
        )
    for name in plan.to_pair:
        report(f"pairing {name!r} (both directions)")
        # A shared epoch per participant-pair keeps the two directed streams
        # legible as one pairing, though each direction is an independent stream.
        epoch = mint_epoch()
        plan.streams.append(
            handshake_direction(
                client, local[name], peer[name], epoch=epoch, report=report
            )
        )
        plan.streams.append(
            handshake_direction(
                client, peer[name], local[name], epoch=epoch, report=report
            )
        )
    report(
        f"paired {len(plan.to_pair)} participant(s), "
        f"{len(plan.streams)} directed stream(s); "
        f"{len(plan.one_sided)} one-sided, {len(plan.refused)} refused"
    )
    return plan


def _rehost(url: str, host: str | None) -> str:
    """Rewrite `url`'s host to `host`, preserving scheme, PORT, path (§4.1
    cross-tailnet addressing). `host` None returns the url unchanged (local
    discovery uses the loopback base_url as-is).

    The rewrite ASSUMES the peer advertised a LOOPBACK base_url (§4.1) that the
    serve posture re-exposes on the tailnet at the SAME port. If the original
    host is NOT loopback, that assumption may not hold — the plugin may have
    advertised a real address on purpose — so we WARN rather than silently
    redirect it to a port that maps to something else on the peer."""
    if not host:
        return url
    parts = urlsplit(url)
    if not _is_loopback(parts.hostname):
        log.warning(
            "rehosting non-loopback base_url %r onto peer host %r (port %s "
            "preserved) — §4.1 assumes plugins advertise LOOPBACK base_urls that "
            "tailscale serve re-exposes 1:1; verify this port maps to this "
            "plugin on the peer",
            url, host, parts.port,
        )
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit((parts.scheme, f"{host}{port}", parts.path, parts.query, parts.fragment))


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _raise_for_status(resp, what: str) -> None:
    if resp.status_code >= 400:
        body = ""
        try:
            body = f" — {resp.json()}"
        except Exception:  # noqa: BLE001 - non-JSON error body
            body = f" — {resp.text[:200]}" if getattr(resp, "text", "") else ""
        if resp.status_code == 403:
            body += (
                " (403: the admin surface is tailnet-gated — is the caller's "
                "peer IP in SNOWLINE_TRUSTED_CIDRS? behind a tailscale-serve → "
                "loopback front the platform config default 100.64.0.0/10 must "
                "be widened to include 127.0.0.0/8,::1 — §5.1)"
            )
        raise PairingError(f"{what} failed: HTTP {resp.status_code}{body}")
