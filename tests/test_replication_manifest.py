"""The manifest `replication` block (replication-continuity.md §4/§9 item 2):
validation and storage at registration time.

Additive and OPTIONAL: absent means the plugin does not replicate (it
degrades alone, per §4). The block is advisory metadata — the registry
stores it as-is, but the gateway and health checker never read it; only the
future pairing step (§5) consumes it. An unknown top-level field rejects the
whole manifest (422), the same fail-loud posture as `UIBlock`
(test_manifest.py)."""

from __future__ import annotations

import anyio
import httpx
import pytest
from pydantic import ValidationError

from snowline_platform import health
from snowline_platform.gateway import discover_upstreams
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry, PluginStatus


def _replication(**overrides) -> dict:
    return {
        "contract_version": 2,
        "ingest_path": "/events/ingest",
        "events": ["decision.recorded", "decision.superseded"],
    } | overrides


def _manifest(replication: dict | None, **kw) -> PluginManifest:
    return PluginManifest(
        name="governance", base_url="http://gov:1", replication=replication, **kw
    )


def test_replication_block_absent_stays_fine():
    m = _manifest(None)
    assert m.replication is None


def test_valid_replication_block_registers():
    m = _manifest(_replication())
    assert m.replication.contract_version == 2
    assert m.replication.ingest_path == "/events/ingest"
    assert m.replication.events == ["decision.recorded", "decision.superseded"]


def test_replication_events_defaults_to_empty_when_omitted():
    replication = _replication()
    del replication["events"]
    m = _manifest(replication)
    assert m.replication.events == []


def test_replication_missing_contract_version_rejected():
    replication = _replication()
    del replication["contract_version"]
    with pytest.raises(ValidationError, match="contract_version"):
        _manifest(replication)


def test_replication_missing_ingest_path_rejected():
    replication = _replication()
    del replication["ingest_path"]
    with pytest.raises(ValidationError, match="ingest_path"):
        _manifest(replication)


def test_unknown_top_level_replication_field_rejected():
    with pytest.raises(ValidationError, match="bogus"):
        _manifest(_replication(bogus=True))


def test_advertised_base_url_absent_defaults_to_none():
    # The field is optional (§4.1): a plugin that never declares it pairs by the
    # port-preserving fallback (#96), so absence must be a clean None.
    m = _manifest(_replication())
    assert m.replication.advertised_base_url is None


def test_advertised_base_url_present_and_stored():
    m = _manifest(_replication(advertised_base_url="http://roam.tailnet:8801"))
    assert m.replication.advertised_base_url == "http://roam.tailnet:8801"


def test_advertised_base_url_trailing_slash_stripped():
    # Shares base_url's trailing-slash strip (_valid_base_url) so the pairing CLI
    # can join paths without doubled slashes.
    m = _manifest(_replication(advertised_base_url="https://roam.tailnet:8801/"))
    assert m.replication.advertised_base_url == "https://roam.tailnet:8801"


def test_advertised_base_url_allows_path_for_path_based_serve_front():
    # A non-empty PATH is allowed on purpose: a path-based serve front (§4.1) is
    # a reason to declare the field, and pairing's ingest/admin suffix is meant
    # to land UNDER that path.
    m = _manifest(_replication(advertised_base_url="https://roam.tailnet/governance/"))
    assert m.replication.advertised_base_url == "https://roam.tailnet/governance"


@pytest.mark.parametrize(
    "bad",
    [
        "roam.tailnet:8801",             # no scheme
        "ftp://roam.tailnet:8801",       # non-http(s) scheme
        "http://roam.tailnet:8801?x=1",  # query — would corrupt the suffixed URL
        "http://roam.tailnet:8801#frag",  # fragment — same
    ],
)
def test_advertised_base_url_malformed_rejected(bad):
    # Stricter than base_url's `_valid_base_url` (scheme + rstrip): because
    # pairing uses this value VERBATIM and suffixes the admin/ingest path onto
    # it (§5), a query or fragment must be rejected too — a suffix onto
    # `...?x=1` / `...#f` corrupts the URL. All fail loud at registration.
    with pytest.raises(ValidationError, match="advertised_base_url"):
        _manifest(_replication(advertised_base_url=bad))


def test_registry_stores_replication_block():
    # The registry stores the whole manifest object, so once the field exists
    # on PluginManifest, storage is automatic — this pins that down as a
    # regression guard rather than an implementation detail of the registry.
    reg = PluginRegistry()
    entry, outcome = reg.upsert(_manifest(_replication()))
    assert outcome == "created"
    assert reg.get("governance").manifest.replication.ingest_path == "/events/ingest"


def test_health_checker_ignores_replication_block():
    # Advisory metadata only (§4): the health checker probes/records status
    # exactly as it would for a manifest with no `replication` block at all —
    # it never reads `health_url`'s composition off anything but
    # base_url/health_path.
    reg = PluginRegistry()
    reg.upsert(_manifest(_replication()))
    entry = reg.get("governance")

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200))
        ) as c:
            return await health.poll_once(reg, c)

    results = anyio.run(go)
    assert results == {"governance": PluginStatus.UP}
    assert reg.get("governance").status is PluginStatus.UP
    # The block itself survives untouched.
    assert reg.get("governance").manifest.replication.contract_version == 2


def test_gateway_ignores_replication_block():
    # discover_upstreams composes routing purely off surfaces/mcp_path/name;
    # a present `replication` block changes nothing about what routes.
    reg = PluginRegistry()
    reg.upsert(_manifest(_replication()))
    reg.set_status("governance", PluginStatus.UP)

    upstreams = discover_upstreams(reg, "main")
    assert [u.plugin_name for u in upstreams] == ["governance"]
