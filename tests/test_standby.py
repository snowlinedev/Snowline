"""The standby readiness surface (issue #259): the READY / DEGRADED / UNKNOWN
rollup, its thresholds, train comparison, report relay + redaction, the
`/standby` + peer-relay routes, and `/health` embedding (which must never
change `/health`'s own status)."""

from __future__ import annotations

import json
import time

import anyio
import httpx
import pytest

from snowline_platform import standby
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginStatus

TAILNET_PEER = "100.64.0.7"
HOTEL_LAN_PEER = "203.0.113.9"
PEER_URL = "http://mbp.tail:8850"


def _app():
    from snowline_platform.app import create_app
    from snowline_platform.trust import Principal, TrustResolver

    class _AlwaysTrust:
        def resolve(self, peer_ip, headers):
            return Principal(id="test-owner", source="test")

    return create_app(resolver=TrustResolver([_AlwaysTrust()]), migrate_on_startup=False)


def _request(app, path, *, peer=TAILNET_PEER):
    result = {}

    async def main():
        transport = httpx.ASGITransport(app=app, client=(peer, 4242))
        async with httpx.AsyncClient(transport=transport, base_url="http://platform") as c:
            result["r"] = await c.get(path)

    anyio.run(main)
    return result["r"]


# --------------------------------------------------------------------------
# Train comparison
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "peer,hub,flag,patch,minor",
    [
        ("v0.5.1", "v0.5.1", "current", 0, 0),
        ("v0.5.1", "v0.5.4", "behind", 3, 0),
        ("v0.4.9", "v0.5.1", "behind", None, 1),
        ("v0.5.2", "v0.5.1", "ahead", None, None),
        ("source", "v0.5.1", "unknown", None, None),
        (None, "v0.5.1", "unknown", None, None),
        ("v0.5.1", None, "unknown", None, None),
    ],
)
def test_compare_trains(peer, hub, flag, patch, minor):
    out = standby.compare_trains(peer, hub)
    assert out["flag"] == flag
    assert out["behind_by_trains"] == patch
    assert out["behind_by_minor"] == minor
    assert (out["peer_train"], out["hub_train"]) == (peer, hub)


# --------------------------------------------------------------------------
# Stream flags + the rollup matrix
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stream,flag",
    [
        ({"pending": 0}, "current"),
        ({"pending": 2, "oldest_pending_age_s": 60}, "lagging"),
        ({"pending": 2, "oldest_pending_age_s": 901}, "behind"),
        ({"pending": 2, "oldest_pending_age_s": None}, "lagging"),
        ({"pending": 0, "version_hold": 1}, "behind"),
        ({"pending": None}, "unknown"),
    ],
)
def test_stream_flag(stream, flag):
    assert standby.stream_flag(stream, 900) == flag


def _code(flag="current", n=0, peer="v0.5.1", hub="v0.5.1", minor=0):
    return {"flag": flag, "behind_by_trains": n, "behind_by_minor": minor,
            "peer_train": peer, "hub_train": hub}


def _s(flag, participant="pm", direction="hub->peer", **kw):
    return {"participant": participant, "direction": direction, "flag": flag,
            "pending": kw.get("pending", 1), "oldest_pending_age_s": kw.get("age", 1000),
            "version_hold": kw.get("hold", 0)}


def _assess(**kw):
    args = {"reachable": True, "code": _code(), "streams": [],
            "snapshot_available": True, "code_behind_max": 0}
    args.update(kw)
    return standby.assess(**args)


def test_rollup_ready_when_code_and_data_current():
    assert _assess(streams=[_s("current"), _s("lagging")]) == ("READY", [])


def test_rollup_degraded_on_code_behind_with_count():
    rollup, reasons = _assess(code=_code("behind", 2, peer="v0.5.1", hub="v0.5.3"))
    assert rollup == "DEGRADED"
    assert reasons == ["code behind by 2 trains: peer runs v0.5.1, hub latest cut v0.5.3"]


def test_code_behind_threshold_tolerates_configured_lag():
    assert _assess(code=_code("behind", 1), code_behind_max=1)[0] == "READY"
    assert _assess(code=_code("behind", 2), code_behind_max=1)[0] == "DEGRADED"
    # Across a minor boundary there is no patch count: always over threshold.
    assert _assess(code=_code("behind", None, minor=1), code_behind_max=5)[0] == "DEGRADED"


def test_rollup_degraded_on_data_behind_and_version_hold():
    rollup, reasons = _assess(streams=[_s("behind", pending=3, age=1200)])
    assert rollup == "DEGRADED"
    assert reasons == ["data behind: pm hub->peer (3 pending, oldest 1200s)"]
    rollup, reasons = _assess(streams=[_s("behind", "platform", "peer->hub", hold=1)])
    assert rollup == "DEGRADED"
    assert reasons == ["data behind: platform peer->hub (1 version hold(s))"]


def test_rollup_unknown_when_unreachable_even_with_known_lag():
    rollup, reasons = _assess(
        reachable=False, error="ConnectError", streams=[_s("behind")],
        code=_code("unknown", None, peer=None),
    )
    assert rollup == "UNKNOWN"
    assert reasons[0] == "peer platform unreachable (ConnectError)"
    assert any(r.startswith("data behind: pm") for r in reasons)


def test_rollup_unknown_when_peer_cannot_report():
    rollup, reasons = _assess(code=_code("unknown", None, peer=None), snapshot_available=False)
    assert rollup == "UNKNOWN"
    assert any("predates the readiness surface" in r for r in reasons)


def test_known_lag_outranks_unknown_when_reachable():
    rollup, _ = _assess(code=_code("unknown", None, peer="source"), streams=[_s("behind")])
    assert rollup == "DEGRADED"


def test_worst():
    assert standby.worst([]) == "UNKNOWN"
    assert standby.worst(["READY", "READY"]) == "READY"
    assert standby.worst(["READY", "UNKNOWN"]) == "UNKNOWN"
    assert standby.worst(["UNKNOWN", "DEGRADED"]) == "DEGRADED"


def test_data_threshold_env(monkeypatch):
    monkeypatch.setenv("SNOWLINE_STANDBY_DATA_BEHIND_AFTER_S", "60")
    assert standby.data_behind_after_seconds() == 60
    monkeypatch.setenv("SNOWLINE_STANDBY_DATA_BEHIND_AFTER_S", "junk")
    assert standby.data_behind_after_seconds() == 900
    monkeypatch.setenv("SNOWLINE_STANDBY_CODE_BEHIND_MAX_TRAINS", "2")
    assert standby.code_behind_max_trains() == 2


# --------------------------------------------------------------------------
# Hub latest cut + reports
# --------------------------------------------------------------------------


def test_hub_latest_cut_takes_the_newest_source(tmp_path):
    manifest = tmp_path / "train.json"
    manifest.write_text(json.dumps({"version": "v0.5.1"}))
    assert standby.hub_latest_cut(None, manifest_path=manifest) == {
        "version": "v0.5.1", "source": "train.json"
    }
    autocut = {"last_cut": {"version": "v0.5.2", "outcome": "ok"}}
    assert standby.hub_latest_cut(autocut, manifest_path=manifest)["version"] == "v0.5.2"
    # A FAILED cut published nothing: it does not count.
    failed = {"last_cut": {"version": "v0.5.9", "outcome": "failed"}}
    assert standby.hub_latest_cut(failed, manifest_path=manifest)["version"] == "v0.5.1"
    # An installed wheel's own train counts; a source checkout's does not.
    assert standby.hub_latest_cut(
        None, manifest_path=tmp_path / "missing.json", running={"version": "v0.6.0"}
    ) == {"version": "v0.6.0", "source": "running"}
    assert standby.hub_latest_cut(
        None, manifest_path=tmp_path / "missing.json", running={"version": "source"}
    ) == {"version": None, "source": None}


def test_redact_strips_secret_shaped_values():
    report = {
        "detail": "pushed with ghp_abcdefghijklmnopqrstuvwxyz0123 to postgresql://u:pw@db/x",
        "secret": "s3cr3t",
        "steps": [{"name": "seed", "detail": "https://user@host/path", "token": None}],
        "auto": True,
        "primary_gateway_url": "http://mini.tail:8850",
    }
    out = standby.redact(report)
    assert out["secret"] == standby.REDACTED
    assert "ghp_" not in out["detail"] and "pw@" not in out["detail"]
    assert "postgresql://[redacted]@db/x" in out["detail"]
    assert out["steps"][0]["detail"] == "https://[redacted]@host/path"
    assert out["steps"][0]["token"] is None  # nothing to hide stays visible
    assert out["auto"] is True
    assert out["primary_gateway_url"] == "http://mini.tail:8850"


def _write_reports(home, *, sync=None, autocut=None, bootstrap=None):
    from snowline_platform.stack import model as stack_model

    base = stack_model.app_support_dir(home)
    base.mkdir(parents=True, exist_ok=True)
    if sync is not None:
        stack_model.report_path(home).write_text(json.dumps(sync))
    if bootstrap is not None:
        stack_model.bootstrap_report_path(home).write_text(json.dumps(bootstrap))
    if autocut is not None:
        (base / "autocut-report.json").write_text(json.dumps(autocut))


def test_read_reports_missing_and_present(tmp_path):
    assert standby.read_reports(tmp_path) == {"sync": None, "bootstrap": None, "autocut": None}
    _write_reports(tmp_path, sync={"outcome": "ok", "train": "v0.5.1", "secret": "x"})
    assert standby.read_reports(tmp_path)["sync"] == {
        "outcome": "ok", "train": "v0.5.1", "secret": standby.REDACTED
    }
    # A corrupt file reads as absent, never raises.
    from snowline_platform.stack import model as stack_model

    stack_model.report_path(tmp_path).write_text("{not json")
    assert standby.read_reports(tmp_path)["sync"] is None


def test_summaries():
    assert standby.sync_summary(None) is None
    assert standby.sync_summary(
        {"outcome": "reverted", "finished_at": "T", "train": "v0.5.1", "detail": "d", "auto": True}
    ) == {"last_outcome": "reverted", "at": "T", "version": "v0.5.1", "detail": "d", "auto": True}
    assert standby.autocut_summary(
        {"outcome": "noop", "finished_at": "T", "last_cut": {"version": "v0.5.1"}}
    ) == {"last_outcome": "noop", "at": "T", "detail": None, "last_cut": {"version": "v0.5.1"}}


# --------------------------------------------------------------------------
# Streams, discovery helpers
# --------------------------------------------------------------------------


def test_streams_from_block_current_and_older_sdk_shapes():
    block = {"outbox": {"oldest_pending_age_s": 50.0, "peers": {
        "MBP.pm": {"pending": 2, "last_error": "HTTP 409 {'reason': 'version_hold'}",
                   "consecutive_failures": 4},
    }}}
    [row] = standby.streams_from_block("pm", block)
    # older SDK: no per-peer age/hold → block age (single peer) + parsed hold
    assert row["oldest_pending_age_s"] == 50.0
    assert row["version_hold"] == 1
    assert row["peer_key"] == "MBP.pm"
    assert standby.streams_from_block("pm", None) == []


def test_peer_instance_and_platform_url_of():
    assert standby.peer_instance("MBP.pm") == "MBP"
    assert standby.peer_instance("http://x/ingest") is None
    assert standby.platform_url_of(f"{PEER_URL}/replication/events/ingest") == PEER_URL
    assert standby.platform_url_of(f"{PEER_URL}/via/pm/replication/events") == PEER_URL
    assert standby.platform_url_of("http://h:1/other/path") == "http://h:1"


def test_assess_peer_splits_directions():
    hub_streams = [
        {"participant": "pm", "peer_key": "MBP.pm", "pending": 0},
        {"participant": "pm", "peer_key": "OTHER.pm", "pending": 9, "oldest_pending_age_s": 9999},
    ]
    fetched = {"reachable": True, "snapshot": {
        "train": {"version": "v0.5.1"}, "services": {"platform": "v0.5.1"},
        "streams": [{"participant": "memory", "peer_key": "mini.memory", "pending": 0},
                    {"participant": "memory", "peer_key": "third.memory", "pending": 5}],
        "front": {"upstream": "spoke", "hub_healthy": False},
    }, "reports": {"sync": {"outcome": "failed", "detail": "gh auth"}}}
    row = standby.assess_peer(
        peer={"peer": "MBP", "platform_url": PEER_URL}, fetched=fetched,
        hub_streams=hub_streams, hub_instance="mini", hub_cut="v0.5.1",
        autocut=None, data_threshold=900, code_max=0,
    )
    dirs = [(s["participant"], s["direction"]) for s in row["data"]["streams"]]
    assert dirs == [("pm", "hub->peer"), ("memory", "peer->hub")]
    assert row["rollup"] == "READY"
    assert row["code"]["services"] == {"platform": "v0.5.1"}
    assert "last stack sync failed: gh auth" in row["notes"]
    assert "the failover front is routing to the spoke" in row["notes"]


def test_sync_trouble_note_survives_the_auto_skip_that_follows_a_revert():
    """A revert at 10:00 then an hourly `skipped` report must not make the
    standby note vanish (the skip detail says 'last attempt reverted')."""
    def note_for(sync):
        row = standby.assess_peer(
            peer={"peer": "MBP", "platform_url": PEER_URL},
            fetched={"reachable": True, "snapshot": {"train": {"version": "v0.5.1"},
                     "services": {"platform": "v0.5.1"}, "streams": []},
                     "reports": {"sync": sync}},
            hub_streams=[], hub_instance="mini", hub_cut="v0.5.1",
            autocut=None, data_threshold=900, code_max=0,
        )
        return row["notes"]
    assert "last stack sync reverted: d" in note_for({"outcome": "reverted", "detail": "d"})
    skipped = note_for({"outcome": "skipped", "detail": "train v0.5.2 last attempt reverted at T; waiting"})
    assert any(n.startswith("last stack sync skipped") and "reverted" in n for n in skipped)
    assert note_for({"outcome": "noop", "detail": None}) == []


def test_assess_peer_falls_back_to_manifest_train():
    row = standby.assess_peer(
        peer={"peer": "MBP", "platform_url": PEER_URL},
        fetched={"reachable": True, "snapshot": None, "manifest": {"train_version": "v0.5.0"}},
        hub_streams=[], hub_instance="mini", hub_cut="v0.5.1",
        autocut=None, data_threshold=900, code_max=0,
    )
    assert row["code"]["peer_train"] == "v0.5.0"
    assert row["rollup"] == "DEGRADED"


# --------------------------------------------------------------------------
# The live computation against the DB + a mocked peer
# --------------------------------------------------------------------------


def _peer_handler(*, train="v0.5.1", streams=None, sync=None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == standby.LOCAL_PATH:
            return httpx.Response(200, json={
                "instance": "MBP", "train": {"version": train},
                "services": {"platform": train},
                "streams": streams or [
                    {"participant": "platform", "peer_key": "mini.platform", "pending": 0},
                ],
                "front": None,
            })
        if path == standby.REPORT_PATH:
            return httpx.Response(200, json={"sync": sync, "bootstrap": None, "autocut": None})
        if path == "/replication/manifest":
            return httpx.Response(200, json={"train_version": train})
        return httpx.Response(404)

    return handler


@pytest.fixture()
def paired(clean_db, monkeypatch, tmp_path):
    """The platform DB holds one live outbound stream to the MBP's platform."""
    from snowline_plugin_sdk.replication import emit

    from snowline_platform.db import session_scope

    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "mini.platform")
    manifest = tmp_path / "train.json"
    manifest.write_text(json.dumps({"version": "v0.5.1"}))
    monkeypatch.setenv("SNOWLINE_TRAIN_MANIFEST", str(manifest))
    with session_scope() as s:
        emit.create_outbound_subscription(
            s, f"{PEER_URL}/replication/events/ingest", "x", ["scope.created"],
            epoch="e1", peer_source_id="MBP.platform",
        )
    return tmp_path


def _compute(app, handler, **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await standby.compute(app, client=c, **kw)

    return anyio.run(go)


def test_compute_ready(paired):
    app = _app()
    out = _compute(app, _peer_handler(sync={"outcome": "ok", "train": "v0.5.1",
                                            "finished_at": "T", "auto": True}))
    assert out["rollup"] == "READY", out["reasons"]
    assert out["instance"] == "mini"
    [peer] = out["peers"]
    assert peer["peer"] == "MBP" and peer["platform_url"] == PEER_URL
    assert peer["reachable"] is True
    assert peer["code"]["flag"] == "current"
    assert peer["sync"]["last_outcome"] == "ok"
    directions = {(s["participant"], s["direction"]) for s in peer["data"]["streams"]}
    assert directions == {("platform", "hub->peer"), ("platform", "peer->hub")}
    assert out["hub"]["latest_cut"] == {"version": "v0.5.1", "source": "train.json"}


def test_compute_degraded_on_a_wedged_plugin_stream(paired):
    """The 2026-10 incident shape: the hub's pm outbox toward the MBP is
    wedged. The standby reads DEGRADED (data behind) — and /health's own
    status says nothing about the hub being red because of it."""
    app = _app()
    reg = app.state.registry
    reg.upsert(PluginManifest(name="pm", base_url="http://pm:1"))
    reg.set_status("pm", PluginStatus.UP, health_detail={"replication": {"outbox": {"peers": {
        "MBP.pm": {"pending": 40, "oldest_pending_age_s": 86400.0, "version_hold": 0,
                   "consecutive_failures": 99, "last_success_at": None,
                   "last_error": "ConnectError", "unreachable_since": "2026-10-01T00:00:00Z"},
    }}}, "train": {"version": "source"}})
    out = _compute(app, _peer_handler())
    assert out["rollup"] == "DEGRADED"
    assert out["reasons"] == ["MBP: data behind: pm hub->peer (40 pending, oldest 86400s)"]


def test_compute_degraded_on_code_behind(paired):
    out = _compute(_app(), _peer_handler(train="v0.4.9"))
    assert out["rollup"] == "DEGRADED"
    assert out["peers"][0]["code"]["behind_by_minor"] == 1


def test_compute_unknown_when_peer_unreachable(paired):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    out = _compute(_app(), down)
    assert out["rollup"] == "UNKNOWN"
    assert out["peers"][0]["reachable"] is False
    assert out["reasons"][0] == "MBP: peer platform unreachable (ConnectError)"


def test_compute_slow_peer_is_bounded_and_unknown(paired):
    async def slow(request):
        await anyio.sleep(10)
        return httpx.Response(200, json={})

    started = time.monotonic()
    out = _compute(_app(), slow, budget=0.3)
    assert time.monotonic() - started < 3
    assert out["rollup"] == "UNKNOWN"
    assert "no answer within" in out["reasons"][0]


def test_compute_no_peers_is_unknown(clean_db):
    out = _compute(_app(), _peer_handler())
    assert out["peers"] == []
    assert out["rollup"] == "UNKNOWN"
    assert out["reasons"] == ["no paired standby peer"]


def test_compute_peer_known_only_from_plugin_streams(clean_db, monkeypatch):
    monkeypatch.setenv("SNOWLINE_REPLICATION_SOURCE_ID", "mini.platform")
    app = _app()
    reg = app.state.registry
    reg.upsert(PluginManifest(name="pm", base_url="http://pm:1"))
    reg.set_status("pm", PluginStatus.UP, health_detail={"replication": {"outbox": {"peers": {
        "MBP.pm": {"pending": 0}}}}})
    out = _compute(app, _peer_handler())
    [peer] = out["peers"]
    assert peer["peer"] == "MBP" and peer["platform_url"] is None
    assert peer["rollup"] == "UNKNOWN"


def _in_one_loop(app, fn):
    """Run `fn(get)` in ONE event loop over an ASGI client (`get(path)` →
    response), so background refreshes scheduled by a request can finish."""
    result = {}

    async def main():
        transport = httpx.ASGITransport(app=app, client=(TAILNET_PEER, 4242))
        async with httpx.AsyncClient(transport=transport, base_url="http://platform") as c:
            result["v"] = await fn(c.get)

    anyio.run(main)
    return result["v"]


def test_refresh_is_cached_once_per_window(monkeypatch):
    calls = []

    async def fake(app, **kw):
        calls.append(1)
        return {"rollup": "READY", "peers": [], "reasons": []}

    monkeypatch.setattr(standby, "compute", fake)
    app = _app()

    async def go(get):
        await standby.for_health(app)  # first: placeholder + schedules
        await app.state.standby_task
        first = await standby.for_health(app)
        await standby.for_health(app)  # fresh: no new task
        return first

    assert _in_one_loop(app, go)["rollup"] == "READY"
    assert len(calls) == 1
    app.state.standby_cache = (time.monotonic() - standby.CACHE_SECONDS - 1, {})

    async def stale(get):
        await standby.for_health(app)
        await app.state.standby_task

    _in_one_loop(app, stale)
    assert len(calls) == 2


def test_a_raising_compute_is_cached_for_the_window(monkeypatch):
    calls = []

    async def boom(app, **kw):
        calls.append(1)
        raise RuntimeError("nope")

    monkeypatch.setattr(standby, "compute", boom)
    app = _app()

    async def go(get):
        await standby.refresh(app)
        second = await standby.for_health(app)  # cached failure: no recompute
        return second

    out = _in_one_loop(app, go)
    assert len(calls) == 1
    assert out["rollup"] == "UNKNOWN"
    assert out["reasons"] == ["standby computation failed: RuntimeError: nope"]
    assert getattr(app.state, "standby_task", None) is None


def test_concurrent_health_callers_share_one_background_refresh(monkeypatch):
    calls = []

    async def slow(app, **kw):
        calls.append(1)
        await anyio.sleep(0.2)
        return {"rollup": "READY", "peers": [], "reasons": []}

    monkeypatch.setattr(standby, "compute", slow)
    app = _app()

    async def go(get):
        for _ in range(5):
            await get("/health")
        await app.state.standby_task

    _in_one_loop(app, go)
    assert len(calls) == 1


# --------------------------------------------------------------------------
# Routes + /health embedding
# --------------------------------------------------------------------------


def test_health_never_awaits_a_hanging_peer_at_the_default_budget(paired):
    """The spoke failover front probes the hub's /health with a 2 s timeout;
    a sleeping standby must not slow /health at all (stale-while-revalidate),
    at the DEFAULT 2 s peer budget."""
    assert standby.PEER_BUDGET_SECONDS == 2.0

    async def hang(request):
        await anyio.sleep(30)
        return httpx.Response(200, json={})

    app = _app()
    app.state.standby_http_client = httpx.AsyncClient(transport=httpx.MockTransport(hang))

    async def go(get):
        times, bodies = [], []
        for _ in range(3):  # first (placeholder) and repeat calls mid-refresh
            t = time.monotonic()
            r = await get("/health")
            times.append(time.monotonic() - t)
            bodies.append(r.json())
        await app.state.standby_task  # the background refresh: bounded ~2 s
        t = time.monotonic()
        after = (await get("/health")).json()
        times.append(time.monotonic() - t)
        await app.state.standby_http_client.aclose()
        return times, bodies, after

    times, bodies, after = _in_one_loop(app, go)
    assert max(times) < 0.5, times
    assert bodies[0]["status"] == "ok"
    assert bodies[0]["standby"]["rollup"] == "UNKNOWN"
    assert bodies[0]["standby"]["reasons"] == [standby.PLACEHOLDER_REASON]
    assert after["standby"]["rollup"] == "UNKNOWN"
    assert "no answer within" in after["standby"]["reasons"][0]


def test_health_status_unchanged_when_standby_degraded(monkeypatch):
    async def degraded(app, **kw):
        return {"rollup": "DEGRADED", "peers": [], "reasons": ["MBP: data behind"]}

    monkeypatch.setattr(standby, "compute", degraded)
    app = _app()
    anyio.run(standby.refresh, app)
    r = _request(app, "/health")
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "ok"
    assert "degraded_reason" not in body
    assert body["standby"]["rollup"] == "DEGRADED"
    assert body["standby"]["reasons"] == ["MBP: data behind"]
    assert set(body["train"]) >= {"version", "editable", "git_sha"}


def test_health_passes_its_replication_block_to_compute(monkeypatch):
    seen = {}

    async def fake(app, **kw):
        seen.update(kw)
        return {"rollup": "READY", "peers": [], "reasons": []}

    monkeypatch.setattr(standby, "compute", fake)
    app = _app()

    async def go(get):
        await get("/health")
        await app.state.standby_task

    _in_one_loop(app, go)
    assert isinstance(seen.get("block"), dict) and "outbox" in seen["block"]


def test_health_survives_a_failing_standby(monkeypatch):
    async def boom(app, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(standby, "compute", boom)
    app = _app()

    async def go(get):
        await get("/health")
        await app.state.standby_task
        return (await get("/health")).json()

    body = _in_one_loop(app, go)
    assert body["status"] == "ok"
    assert body["standby"]["rollup"] == "UNKNOWN"
    assert body["standby"]["reasons"] == ["standby computation failed: RuntimeError: nope"]


def test_standby_route_waits_for_the_first_block(monkeypatch):
    async def ready(app, **kw):
        await anyio.sleep(0.05)
        return {"rollup": "READY", "peers": [], "reasons": []}

    monkeypatch.setattr(standby, "compute", ready)
    r = _request(_app(), "/standby")
    assert r.status_code == 200 and r.json()["rollup"] == "READY"


def test_stack_report_is_gated_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("SNOWLINE_STANDBY_HOME", str(tmp_path))
    _write_reports(
        tmp_path,
        sync={"outcome": "ok", "train": "v0.5.1", "detail": "via https://bob:hunter2@x/y"},
        autocut={"outcome": "noop", "last_cut": {"version": "v0.5.1"}},
    )
    app = _app()
    assert _request(app, standby.REPORT_PATH, peer=HOTEL_LAN_PEER).status_code == 403
    body = _request(app, standby.REPORT_PATH, peer="127.0.0.1").json()
    assert body["sync"]["outcome"] == "ok"
    assert "hunter2" not in json.dumps(body)
    assert body["autocut"]["last_cut"]["version"] == "v0.5.1"
    assert body["bootstrap"] is None


def test_standby_local_is_gated_and_reports_train_and_streams(monkeypatch):
    app = _app()
    reg = app.state.registry
    reg.upsert(PluginManifest(name="memory", base_url="http://memory:1"))
    reg.set_status("memory", PluginStatus.UP, health_detail={
        "train": {"version": "v0.5.1"},
        "replication": {"outbox": {"peers": {"mini.memory": {"pending": 0}}}},
    })
    assert _request(app, standby.LOCAL_PATH, peer=HOTEL_LAN_PEER).status_code == 403
    body = _request(app, standby.LOCAL_PATH).json()
    assert body["services"]["memory"] == "v0.5.1"
    assert "platform" in body["services"]
    assert {"participant": "memory", "peer_key": "mini.memory"}.items() <= body["streams"][-1].items()
    assert body["front"] is None


def test_standby_local_relays_the_front(monkeypatch):
    monkeypatch.setenv("SNOWLINE_FRONT_HEALTH_URL", "http://127.0.0.1:1/front/health")

    async def go():
        def handler(request):
            return httpx.Response(200, json={
                "status": "ok", "upstream": "hub", "hub_healthy": True,
                "last_flip_at": None, "flips": 0, "hub_url": "h", "spoke_url": "s",
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await standby.fetch_front(c)

    front = anyio.run(go)
    assert front["reachable"] is True and front["upstream"] == "hub"
    assert front["hub_healthy"] is True

    async def go_down():
        def handler(request):
            raise httpx.ConnectError("x", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await standby.fetch_front(c)

    assert anyio.run(go_down) == {"reachable": False, "error": "ConnectError"}


def test_self_manifest_carries_train_version():
    from snowline_platform import replication

    assert replication.manifest_payload()["train_version"] == standby.platform_train()["version"]


# --------------------------------------------------------------------------
# The poller relays the health-body slice
# --------------------------------------------------------------------------


def test_poller_stores_health_detail_and_clears_it_on_down():
    from snowline_platform import health
    from snowline_platform.registry import PluginRegistry

    reg = PluginRegistry()
    reg.upsert(PluginManifest(name="memory", base_url="http://memory:1"))
    body = {"status": "ok", "train": {"version": "v0.5.1"},
            "replication": {"status": "ok", "outbox": {"peers": {}}}, "other": 1}
    state = {"up": True}

    def handler(request):
        if state["up"]:
            return httpx.Response(200, json=body)
        return httpx.Response(503)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await health.poll_once(reg, c)

    anyio.run(go)
    assert reg.get("memory").health_detail == {
        "replication": body["replication"], "train": body["train"]
    }
    state["up"] = False
    anyio.run(go)
    assert reg.get("memory").status is PluginStatus.DOWN
    assert reg.get("memory").health_detail is None


# --------------------------------------------------------------------------
# Review follow-ons: partial peer answers, refusal vs old peer, bad shapes
# --------------------------------------------------------------------------


def test_a_hanging_stack_report_does_not_lose_the_snapshot(paired):
    base = _peer_handler()

    async def handler(request):
        if request.url.path == standby.REPORT_PATH:
            await anyio.sleep(30)
        return base(request)

    started = time.monotonic()
    out = _compute(_app(), handler, budget=0.5)
    assert time.monotonic() - started < 3
    [peer] = out["peers"]
    assert peer["rollup"] == "READY", peer["reasons"]
    assert peer["sync"] is None
    assert peer["code"]["flag"] == "current"


def test_a_refusing_peer_is_named_as_refused_not_old(paired):
    def handler(request):
        if request.url.path == "/replication/manifest":
            return httpx.Response(200, json={"train_version": "v0.5.1"})
        return httpx.Response(403, json={"detail": "untrusted peer"})

    [peer] = _compute(_app(), handler)["peers"]
    assert peer["rollup"] == "UNKNOWN"
    assert any("peer refused /standby/local (HTTP 403): untrusted source IP" in r
               for r in peer["reasons"])
    assert not any("predates" in r for r in peer["reasons"])


def test_an_old_peer_404_says_predates(paired):
    def handler(request):
        if request.url.path == "/replication/manifest":
            return httpx.Response(200, json={"train_version": "v0.5.1"})
        return httpx.Response(404)

    [peer] = _compute(_app(), handler)["peers"]
    assert any("predates the readiness surface" in r for r in peer["reasons"])


@pytest.mark.parametrize(
    "snapshot",
    [
        {"train": "v0.5.1", "services": "x", "streams": "y", "front": "z"},
        {"train": {"version": 5}, "streams": [1, "a", {"no": "participant"}]},
        {"train": None, "streams": None},
    ],
)
def test_malformed_peer_snapshot_never_raises(snapshot):
    row = standby.assess_peer(
        peer={"peer": "MBP", "platform_url": PEER_URL},
        fetched={"reachable": True, "snapshot": snapshot, "manifest": "bad", "reports": ["x"]},
        hub_streams=[], hub_instance="mini", hub_cut="v0.5.1",
        autocut=None, data_threshold=900, code_max=0,
    )
    assert row["rollup"] in {"READY", "UNKNOWN"}
    assert row["front"] is None and row["sync"] is None


def test_string_train_is_read_as_the_version():
    row = standby.assess_peer(
        peer={"peer": "MBP", "platform_url": PEER_URL},
        fetched={"reachable": True, "snapshot": {"train": "v0.5.1", "streams": []}},
        hub_streams=[], hub_instance="mini", hub_cut="v0.5.1",
        autocut=None, data_threshold=900, code_max=0,
    )
    assert row["code"]["flag"] == "current"


def test_peer_ahead_of_the_cut_is_ready_with_a_note():
    row = standby.assess_peer(
        peer={"peer": "MBP", "platform_url": PEER_URL},
        fetched={"reachable": True, "snapshot": {"train": {"version": "v0.5.2"}, "streams": []}},
        hub_streams=[], hub_instance="mini", hub_cut="v0.5.1",
        autocut=None, data_threshold=900, code_max=0,
    )
    assert row["rollup"] == "READY"
    assert any("hub's cut record may be stale" in n for n in row["notes"])


def test_redact_covers_connection_strings_and_keeps_flags():
    out = standby.redact({
        "dsn": "host=x password=y", "database_url": "postgresql:///x",
        "seed_config": {"a": 1}, "cookie": "c", "session_id": "s",
        "reseed": False, "outcome": "ok",
    })
    for k in ("dsn", "database_url", "seed_config", "cookie", "session_id"):
        assert out[k] == standby.REDACTED, k
    assert out["reseed"] is False and out["outcome"] == "ok"


def test_a_hung_refresh_times_out_into_a_cached_unknown(monkeypatch):
    calls = []

    async def hang(app, **kw):
        calls.append(1)
        await anyio.sleep(30)

    monkeypatch.setattr(standby, "compute", hang)
    monkeypatch.setattr(standby, "REFRESH_DEADLINE_SECONDS", 0.2)
    app = _app()

    async def go(get):
        started = time.monotonic()
        out = await standby.refresh(app)
        elapsed = time.monotonic() - started
        again = await standby.for_health(app)  # cached: no new refresh
        return out, elapsed, again

    out, elapsed, again = _in_one_loop(app, go)
    assert elapsed < 2
    assert out["rollup"] == "UNKNOWN"
    assert out["reasons"] == ["standby refresh timed out (> 0.2s)"]
    assert again["reasons"] == out["reasons"]
    assert len(calls) == 1


def test_served_block_carries_its_age(monkeypatch):
    async def ready(app, **kw):
        return {"rollup": "READY", "peers": [], "reasons": []}

    monkeypatch.setattr(standby, "compute", ready)
    app = _app()

    async def go(get):
        placeholder = await standby.for_health(app)
        await app.state.standby_task
        return placeholder

    placeholder = _in_one_loop(app, go)
    assert placeholder["age_s"] is None
    app.state.standby_cache = (time.monotonic() - 4, app.state.standby_cache[1])
    body = _request(app, "/health").json()["standby"]
    assert body["rollup"] == "READY"
    assert 3.9 <= body["age_s"] < 10
    assert "age_s" not in app.state.standby_cache[1]  # the cache itself is untouched


def test_lifespan_shutdown_cancels_an_in_flight_refresh(monkeypatch):
    from starlette.testclient import TestClient

    async def hang(app, **kw):
        await anyio.sleep(60)

    monkeypatch.setattr(standby, "compute", hang)
    app = _app()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        task = app.state.standby_task
        assert not task.done()
    assert task.cancelled()
