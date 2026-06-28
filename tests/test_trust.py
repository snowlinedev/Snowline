"""Trust layer — the configurable CIDR gate and the pluggable resolver seam."""

from snowline_platform.trust import CidrTrustProvider, Principal, TrustResolver


def test_cidr_provider_trusts_in_range():
    provider = CidrTrustProvider(["100.64.0.0/10"])
    principal = provider.resolve("100.81.176.75", {})
    assert principal is not None
    assert principal.id == "owner"
    assert principal.source == "tailnet-cidr"


def test_cidr_provider_rejects_out_of_range():
    provider = CidrTrustProvider(["100.64.0.0/10"])
    assert provider.resolve("8.8.8.8", {}) is None


def test_cidr_provider_is_configurable_narrowing():
    # A narrower trusted set (one host) excludes other tailnet IPs.
    provider = CidrTrustProvider(["100.81.176.75/32"])
    assert provider.resolve("100.81.176.75", {}) is not None
    assert provider.resolve("100.81.176.76", {}) is None


def test_cidr_provider_handles_malformed_ip():
    provider = CidrTrustProvider(["100.64.0.0/10"])
    assert provider.resolve("not-an-ip", {}) is None


def test_resolver_runs_providers_in_order_first_match_wins():
    # A fake token provider (the future-OAuth shape) prepended ahead of the CIDR
    # gate proves the pluggable seam + order: token-first, CIDR fallback.
    class FakeTokenProvider:
        source = "oauth"

        def resolve(self, peer_ip, headers):
            if headers.get("authorization"):
                return Principal(id="alice", source="oauth")
            return None

    resolver = TrustResolver(
        [FakeTokenProvider(), CidrTrustProvider(["100.64.0.0/10"])]
    )

    # Token present (even off-tailnet) -> named user via the token provider.
    named = resolver.resolve("8.8.8.8", {"authorization": "Bearer x"})
    assert named is not None and named.id == "alice"

    # No token, on the tailnet -> owner via the CIDR fallback.
    owner = resolver.resolve("100.81.176.75", {})
    assert owner is not None and owner.id == "owner"

    # No token, off the tailnet -> untrusted.
    assert resolver.resolve("8.8.8.8", {}) is None
