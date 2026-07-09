"""The platform trust layer — resolve a request to a trusted Principal, or reject.

Access control is a single PLUGGABLE seam: the request pipeline asks the
`TrustResolver` "is this request trusted, and who is it?". v1 ships ONE provider
— a configurable trusted-CIDR network gate (the tailnet range PLUS loopback,
both trusted as owner network-position by deliberate policy, governance
decision 35546152 — not a tailnet-only default) — and the resolver runs
providers in order, so OAuth (a per-user token provider) slots in later
WITHOUT any change downstream.

Two layers stay separate: the NETWORK gate (which CIDRs may reach the platform)
lives here; APP AUTHZ (what a Principal may *do*) keys on the returned Principal
elsewhere. OAuth only ever adds an identity provider here; it never touches the
network gate. Public exposure never widens this gate either — it authenticates
at an edge front instead (Snowline#120).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Principal:
    """Who the platform believes is making a request, and how it knows.

    `id` is the stable identity — v1: the single ``"owner"``; with OAuth: the
    user subject. `source` records which provider vouched (for audit). `attrs`
    carries provider-specific extras (a tailnet login, an OAuth scope set, ...)
    so the rest of the platform never needs to know the provider.
    """

    id: str
    source: str
    attrs: Mapping[str, object] | None = None


@runtime_checkable
class TrustProvider(Protocol):
    """Turns a request's signals into a trusted `Principal`, or ``None``.

    ``None`` means "I can't vouch for this request" — the resolver tries the
    next provider. A `Principal` means "trusted, and here's who." Implementations
    must be cheap and side-effect-free (called once per request).
    """

    def resolve(
        self, peer_ip: str, headers: Mapping[str, str]
    ) -> Principal | None: ...


class CidrTrustProvider:
    """v1: trust by source-IP membership in a configured set of CIDRs.

    The tailnet lives in a known range (Tailscale's CGNAT ``100.64.0.0/10``),
    and the default set also includes loopback — both are trusted as the
    single ``owner`` on single-owner machines where possession of the box
    already implies possession of its tailnet identity (governance decision
    35546152; loopback is not an incidental widening). Zero per-client config,
    which preserves the SSH/daily flow (the opposite of a per-client-header
    scheme). This is a NETWORK gate: it proves "on the trusted network", not
    "which user" — sufficient for a single-user v1, and only ever narrowed by
    config, never by folding in a non-owner tailnet node's traffic (those get
    ACL-scoped at the network layer instead). Per-user identity (Tailscale
    serve-headers / LocalAPI WhoIs, or OAuth) is a later provider behind the
    same seam.
    """

    source = "tailnet-cidr"

    def __init__(
        self, trusted_cidrs: list[str], owner_id: str = "owner"
    ) -> None:
        self._nets = [
            ipaddress.ip_network(c, strict=False) for c in trusted_cidrs
        ]
        self._owner_id = owner_id

    def resolve(
        self, peer_ip: str, headers: Mapping[str, str]
    ) -> Principal | None:
        try:
            ip = ipaddress.ip_address(peer_ip)
        except ValueError:
            return None
        if any(ip in net for net in self._nets):
            return Principal(id=self._owner_id, source=self.source)
        return None


class TrustResolver:
    """Runs trust providers in order; the first to vouch wins.

    Order is load-bearing: when an OAuth provider is added it goes FIRST, so a
    request carrying a token gets its real per-user identity and the CIDR gate is
    the zero-config fallback for the tailnet path. Returns the `Principal`, or
    ``None`` if no provider trusts the request (the caller rejects).
    """

    def __init__(self, providers: list[TrustProvider]) -> None:
        self._providers = list(providers)

    def resolve(
        self, peer_ip: str, headers: Mapping[str, str]
    ) -> Principal | None:
        for provider in self._providers:
            principal = provider.resolve(peer_ip, headers)
            if principal is not None:
                return principal
        return None
