"""The STANDBY READINESS surface (issue #259): "can I fail over right now?"
answered as one field, per paired peer.

The MBP spoke is a warm standby (it keeps Sean working when the hub mini loses
power). Whether that is true at any moment depends on two things the plain
`/health` aggregate never showed: is the peer's CODE current (does it run the
hub's latest train?) and is its DATA current (are the replication streams
between the two instances flowing, in both directions?). The hub outbox toward
the MBP sat wedged for weeks (#235) with `/health` green throughout.

What this module computes — the `standby` block, served at `GET /standby` and
embedded as `health.standby` on `/health`:

    {"computed_at", "instance": <this instance id>,
     "hub": {"train": <running train>, "latest_cut": {version, source},
             "autocut": {last_outcome, at, detail, last_cut,
                         running, next_tick_at, host_enabled} | None},
     "thresholds": {"data_behind_after_s", "code_behind_max_trains"},
     "peers": [{
        "peer": "MBP", "platform_url", "reachable", "error",
        "code": {peer_train, hub_train, behind_by_trains, behind_by_minor,
                 flag, services},
        "data": {"streams": [{participant, direction: "hub->peer"|"peer->hub",
                              pending, oldest_pending_age_s, last_success_at,
                              unreachable_since, consecutive_failures,
                              version_hold, last_error, flag}]},
        "sync": {last_outcome, at, version, detail, auto} | None,
        "autocut": <hub.autocut>,
        "front": {upstream, hub_healthy, last_flip_at, flips} | None,
        "rollup": "READY"|"DEGRADED"|"UNKNOWN", "reasons": [..], "notes": [..]}],
     "rollup": <worst over peers>, "reasons": [..],
     "age_s": <seconds since computed, added when served from the cache>}

"hub" is the instance ANSWERING (the surface is symmetric: a spoke asked the
same question reports on its hub). Rollup rules:

  * UNKNOWN  — the peer's platform did not answer within the budget, or it
    answered but cannot report its code/data (a peer predating #259), or no
    peer is paired at all.
  * DEGRADED — reachable, and the code lags (peer train more than
    `code_behind_max_trains` behind the hub's latest cut; default 0, i.e. any
    lag) or any stream is "behind": pending events older than
    `data_behind_after_s` (default 900 = the SDK's own unreachable threshold)
    or any version hold.
  * READY    — reachable, code current, every stream current or only briefly
    lagging.

The standby rollup NEVER feeds `/health`'s own `status` — that stays truthful
about this instance alone — but it is its own top-level field there so a
lagging peer cannot be missed either.

Cost: STALE-WHILE-REVALIDATE. `/health` (and `/standby`) answer at once
from the last computed block — an UNKNOWN placeholder before the first — and,
when it is older than `CACHE_SECONDS`, start ONE background refresh. `/health`
never awaits a peer: the spoke failover front probes the hub's `/health` with
a 2 s timeout, and a sleeping standby must not be able to trip it. The
background refresh bounds all peer fetches by one `PEER_BUDGET_SECONDS`
deadline (each GET by its own slightly shorter one), so a slow or powered-down
peer reads as UNKNOWN; a raising computation is cached as an UNKNOWN block for
the window too, never retried per caller.

The peer side of the relay is two tailnet-gated, read-only routes every
platform serves: `GET /standby/local` (this instance's train, per-service
trains and outbox streams — what a peer needs for the "peer->hub" half) and
`GET /stack/report` (the sync / bootstrap / autocut reports on disk, with
anything secret-shaped redacted).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx
from fastapi import APIRouter, Request
from snowline_plugin_sdk.replication.admin import _require_trusted
from snowline_plugin_sdk.replication.envelope import (
    REFUSAL_VERSION_HOLD,
    is_backfill_source,
)
from snowline_plugin_sdk.replication.health import replication_health_from_scope
from snowline_plugin_sdk.replication.models import ReplicationSubscription
from snowline_plugin_sdk.train import SOURCE, cached_running_train, parse_train
from sqlalchemy import select

log = logging.getLogger("snowline_platform.standby")

READY = "READY"
DEGRADED = "DEGRADED"
UNKNOWN = "UNKNOWN"

HUB_TO_PEER = "hub->peer"
PEER_TO_HUB = "peer->hub"

CACHE_SECONDS = 10.0
# A refresh runs in the background, but it must still end: one stuck DB
# call or peer read past this is cached as an UNKNOWN "timed out" block.
REFRESH_DEADLINE_SECONDS = 30.0
PEER_BUDGET_SECONDS = 2.0
FRONT_TIMEOUT_SECONDS = 0.5
DEFAULT_DATA_BEHIND_AFTER_S = 900.0
DEFAULT_CODE_BEHIND_MAX_TRAINS = 0

LOCAL_PATH = "/standby/local"
REPORT_PATH = "/stack/report"
PLATFORM_INGEST_PATH = "/replication/events/ingest"
VIA_SEGMENT = "/via/"

PLATFORM_PACKAGE = "snowline-platform"

# The autocut outcomes that mean "a train was published" (release/autocut.py).
_PUBLISHED_OUTCOMES = {"ok", "published_unrecorded", "recorded"}
_SYNC_TROUBLE = {"failed", "reverted", "needs_attention"}


# --------------------------------------------------------------------------
# Thresholds / config (read live so tests and env changes are honored)
# --------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def data_behind_after_seconds() -> float:
    return _env_float(
        "SNOWLINE_STANDBY_DATA_BEHIND_AFTER_S", DEFAULT_DATA_BEHIND_AFTER_S
    )


def code_behind_max_trains() -> int:
    return int(
        _env_float(
            "SNOWLINE_STANDBY_CODE_BEHIND_MAX_TRAINS", DEFAULT_CODE_BEHIND_MAX_TRAINS
        )
    )


def reports_home() -> Path:
    """The home whose app-support dir holds the sync/bootstrap/autocut reports
    (`stack.model.app_support_dir`). `SNOWLINE_STANDBY_HOME` overrides (tests,
    a service running under a different account than the one that syncs)."""
    raw = os.environ.get("SNOWLINE_STANDBY_HOME")
    return Path(raw) if raw else Path.home()


def train_manifest_path() -> Path | None:
    """The hub's latest-cut record, release/train.json: `SNOWLINE_TRAIN_MANIFEST`
    when set, else the one in the checkout this package runs from (the hub runs
    from source — src/snowline_platform → repo root). None when neither exists
    (an installed wheel carries no train.json; its own version is the train)."""
    raw = os.environ.get("SNOWLINE_TRAIN_MANIFEST")
    if raw:
        return Path(raw)
    here = Path(__file__).resolve()
    candidate = here.parents[2] / "release" / "train.json"
    return candidate if candidate.is_file() else None


def front_health_url() -> str | None:
    """Where THIS instance's failover front (item 12065698) answers
    `GET /front/health`, if one runs here. Optional: unset → no front block."""
    raw = os.environ.get("SNOWLINE_FRONT_HEALTH_URL", "").strip()
    return raw or None


def platform_train() -> dict:
    return cached_running_train(PLATFORM_PACKAGE)


# --------------------------------------------------------------------------
# Reports on disk (read-only) + redaction
# --------------------------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"(secret|token|password|passwd|credential|api[_-]?key|private[_-]?key"
    r"|authorization|(^|_)auth($|_)|dsn|database_url|seed|cookie|session)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_URL_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s@:]+(:[^/\s@]*)?@", re.IGNORECASE)
REDACTED = "[redacted]"


def redact(value):
    """Strip anything secret-shaped from a report before it leaves the machine:
    values under secret-named keys, token-shaped strings, and URL userinfo
    (`postgresql://user:pw@host` → `postgresql://[redacted]@host`). The reports
    carry URLs, versions and outcomes only today — this is the belt to that
    braces, so a future report field can't leak through the relay."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if (
                isinstance(k, str)
                and _SECRET_KEY_RE.search(k)
                and not isinstance(v, bool)  # a flag (`reseed`) is no secret
                and v not in (None, "", [], {})
            ):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        text = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}{REDACTED}@", value)
        return _SECRET_VALUE_RE.sub(REDACTED, text)
    return value


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_reports(home: Path | None = None) -> dict:
    """{"sync", "bootstrap", "autocut"}: each the latest report JSON in the
    app-support dir (None when absent/unreadable), redacted."""
    from snowline_platform.stack import model as stack_model

    home = home or reports_home()
    base = stack_model.app_support_dir(home)
    return {
        "sync": redact(_read_json(stack_model.report_path(home))),
        "bootstrap": redact(_read_json(stack_model.bootstrap_report_path(home))),
        "autocut": redact(_read_json(base / "autocut-report.json")),
    }


def sync_summary(report: dict | None) -> dict | None:
    if not isinstance(report, dict):
        return None
    return {
        "last_outcome": report.get("outcome"),
        "at": report.get("finished_at"),
        "version": report.get("train"),
        "detail": report.get("detail"),
        "auto": report.get("auto"),
    }


def autocut_summary(report: dict | None) -> dict | None:
    if not isinstance(report, dict):
        return None
    last = report.get("last_cut")
    return {
        "last_outcome": report.get("outcome"),
        "at": report.get("finished_at"),
        "detail": report.get("detail"),
        "last_cut": last if isinstance(last, dict) else None,
    }


def with_live_host(autocut: dict | None, host: object | None) -> dict | None:
    """Merge the in-process auto-cut host's LIVE state (app.state.autocut_host,
    release/autocut_host.py) into the report summary: `running` answers "is a
    cut running right now?", which the on-disk report cannot (it is written
    when a run ends). No host (a test app, an older platform) -> unchanged."""
    to_dict = getattr(host, "to_dict", None)
    if to_dict is None:
        return autocut
    live = to_dict()
    merged = dict(autocut or {"last_outcome": None, "at": None, "detail": None, "last_cut": None})
    merged["running"] = bool(live.get("running"))
    merged["next_tick_at"] = live.get("next_tick_at")
    merged["host_enabled"] = bool(live.get("enabled"))
    return merged


def _fmt(t: tuple[int, int, int]) -> str:
    return f"v{t[0]}.{t[1]}.{t[2]}"


def hub_latest_cut(
    autocut: dict | None, *, manifest_path: Path | None = None, running: dict | None = None
) -> dict:
    """The newest train this hub has cut: the max of release/train.json's
    `version`, the autocut report's published `last_cut.version`, and (an
    installed wheel) the running train. `source` names where it came from;
    `version` is None when none of them names a train."""
    candidates: list[tuple[tuple[int, int, int], str]] = []
    path = manifest_path if manifest_path is not None else train_manifest_path()
    if path is not None:
        data = _read_json(path) or {}
        parsed = parse_train(data.get("version"))
        if parsed:
            candidates.append((parsed, "train.json"))
    last = (autocut or {}).get("last_cut") or {}
    if last.get("outcome") in _PUBLISHED_OUTCOMES:
        parsed = parse_train(last.get("version"))
        if parsed:
            candidates.append((parsed, "autocut"))
    parsed = parse_train((running or {}).get("version"))
    if parsed:
        candidates.append((parsed, "running"))
    if not candidates:
        return {"version": None, "source": None}
    best = max(candidates, key=lambda c: c[0])
    return {"version": _fmt(best[0]), "source": best[1]}


# --------------------------------------------------------------------------
# Pure assessment (unit-tested as a matrix)
# --------------------------------------------------------------------------


def compare_trains(peer_train: str | None, hub_train: str | None) -> dict:
    """Peer vs hub train by semver order. `behind_by_trains` counts PATCH
    trains when both share major.minor (the auto-cut steps patch); across a
    minor boundary it is None and `behind_by_minor` carries the minor gap —
    the raw versions are always reported, so the number never has to be
    trusted blind. flag: current | behind | ahead | unknown."""
    p, h = parse_train(peer_train), parse_train(hub_train)
    out = {
        "peer_train": peer_train,
        "hub_train": hub_train,
        "behind_by_trains": None,
        "behind_by_minor": None,
        "flag": "unknown",
    }
    if p is None or h is None:
        return out
    if p == h:
        out.update(flag="current", behind_by_trains=0, behind_by_minor=0)
    elif p > h:
        out["flag"] = "ahead"
    else:
        out["flag"] = "behind"
        if p[:2] == h[:2]:
            out["behind_by_trains"] = h[2] - p[2]
            out["behind_by_minor"] = 0
        elif p[0] == h[0]:
            out["behind_by_minor"] = h[1] - p[1]
    return out


def stream_flag(stream: dict, data_behind_after_s: float) -> str:
    """current (nothing pending) | lagging (pending, younger than the
    threshold) | behind (pending past the threshold, or a version hold) |
    unknown (the outbox could not be read)."""
    if stream.get("version_hold"):
        return "behind"
    pending = stream.get("pending")
    if pending is None:
        return "unknown"
    if pending == 0:
        return "current"
    age = stream.get("oldest_pending_age_s")
    if age is None:
        return "lagging"
    return "behind" if age > data_behind_after_s else "lagging"


def assess(
    *,
    reachable: bool,
    code: dict,
    streams: list[dict],
    snapshot_available: bool,
    code_behind_max: int,
    error: str | None = None,
    snapshot_missing: str | None = None,
) -> tuple[str, list[str]]:
    """The per-peer rollup + its reasons (rules in the module docstring)."""
    degraded: list[str] = []
    unknown: list[str] = []
    if not reachable:
        unknown.append(
            "peer platform unreachable" + (f" ({error})" if error else "")
        )
    flag = code.get("flag")
    if flag == "behind":
        n = code.get("behind_by_trains")
        lag_over = n is None or n > code_behind_max
        if lag_over:
            gap = (
                f"{n} train{'s' if n != 1 else ''}"
                if n is not None
                else f"{code.get('behind_by_minor') or '?'} minor"
            )
            degraded.append(
                f"code behind by {gap}: peer runs {code.get('peer_train')}, "
                f"hub latest cut {code.get('hub_train')}"
            )
    elif flag == "unknown" and reachable:
        unknown.append(
            f"code currency unknown: peer train {code.get('peer_train')!r}, "
            f"hub latest cut {code.get('hub_train')!r}"
        )
    for s in streams:
        f = s.get("flag")
        if f == "behind":
            what = (
                f"{s.get('version_hold')} version hold(s)"
                if s.get("version_hold")
                else f"{s.get('pending')} pending, oldest {int(s.get('oldest_pending_age_s') or 0)}s"
            )
            degraded.append(f"data behind: {s['participant']} {s['direction']} ({what})")
        elif f == "unknown" and reachable:
            unknown.append(f"data unknown: {s['participant']} {s['direction']} outbox unreadable")
    if reachable and not snapshot_available:
        unknown.append(
            snapshot_missing
            or "peer does not report its outbox/train (predates the readiness "
            "surface, #259) — peer->hub data unknown"
        )
    if not reachable:
        return UNKNOWN, unknown + degraded
    if degraded:
        return DEGRADED, degraded + unknown
    if unknown:
        return UNKNOWN, unknown
    return READY, []


def worst(rollups: list[str]) -> str:
    if not rollups:
        return UNKNOWN
    if DEGRADED in rollups:
        return DEGRADED
    if UNKNOWN in rollups:
        return UNKNOWN
    return READY


# --------------------------------------------------------------------------
# Local streams (this instance's outboxes) and the /standby/local snapshot
# --------------------------------------------------------------------------


def peer_instance(key: str | None) -> str | None:
    """`MBP.pm` → `MBP`; an ingest-URL key (a stream with no reverse
    direction paired) names no instance → None."""
    if not key or "://" in key:
        return None
    return key.split(".", 1)[0]


def streams_from_block(participant: str, block: dict | None) -> list[dict]:
    """One stream row per peer in an SDK `replication` health block."""
    if not isinstance(block, dict):
        return []
    outbox = block.get("outbox") or {}
    peers = outbox.get("peers") or {}
    unavailable = outbox.get("unavailable")
    rows = []
    for key, p in sorted(peers.items()):
        if not isinstance(p, dict):
            continue
        pending = p.get("pending")
        oldest = p.get("oldest_pending_age_s")
        if "oldest_pending_age_s" not in p and pending and len(peers) == 1:
            # An SDK predating per-peer ages: the block-level age is this
            # peer's when it is the only one.
            oldest = outbox.get("oldest_pending_age_s")
        hold = p.get("version_hold")
        if hold is None and pending and REFUSAL_VERSION_HOLD in str(p.get("last_error") or ""):
            hold = 1  # an older SDK: the head's last refusal names the hold
        rows.append(
            {
                "participant": participant,
                "peer_key": key,
                "pending": pending,
                "oldest_pending_age_s": oldest,
                "last_success_at": p.get("last_success_at"),
                "unreachable_since": p.get("unreachable_since"),
                "consecutive_failures": p.get("consecutive_failures"),
                "version_hold": hold or 0,
                "last_error": p.get("last_error"),
                "unavailable": unavailable,
            }
        )
    return rows


def local_streams(platform_block: dict | None, registry) -> list[dict]:
    """Every outbound stream this instance knows: the platform's own (its
    replication block, computed now) and each registered plugin's (the block
    its last health poll returned, `RegisteredPlugin.health_detail`)."""
    from snowline_platform.platform_tools import PLATFORM_PLUGIN_NAME

    rows = streams_from_block(PLATFORM_PLUGIN_NAME, platform_block)
    for entry in registry.list():
        name = entry.manifest.name
        if name == PLATFORM_PLUGIN_NAME:
            continue
        detail = entry.health_detail or {}
        rows.extend(streams_from_block(name, detail.get("replication")))
    return rows


def local_services(registry) -> dict:
    """{service: its reported running train (or None)} for every registered
    plugin, plus the platform itself."""
    from snowline_platform.platform_tools import PLATFORM_PLUGIN_NAME

    out = {PLATFORM_PLUGIN_NAME: platform_train().get("version")}
    for entry in registry.list():
        name = entry.manifest.name
        if name == PLATFORM_PLUGIN_NAME:
            continue
        train = (entry.health_detail or {}).get("train") or {}
        out[name] = train.get("version") if isinstance(train, dict) else None
    return out


def instance_id() -> str | None:
    source = os.environ.get("SNOWLINE_REPLICATION_SOURCE_ID") or ""
    return source.split(".", 1)[0] or None


async def fetch_front(client: httpx.AsyncClient | None = None) -> dict | None:
    """This instance's failover-front status (optional; see
    `front_health_url`). Any failure → `{"reachable": False}`."""
    url = front_health_url()
    if url is None:
        return None
    own = client is None
    client = client or httpx.AsyncClient(timeout=FRONT_TIMEOUT_SECONDS)
    try:
        resp = await client.get(url, timeout=FRONT_TIMEOUT_SECONDS)
        body = resp.json() if resp.is_success else None
    except Exception as exc:  # noqa: BLE001 - optional, never fatal
        return {"reachable": False, "error": type(exc).__name__}
    finally:
        if own:
            await client.aclose()
    if not isinstance(body, dict):
        return {"reachable": False, "error": f"HTTP {resp.status_code}"}
    return {
        "reachable": True,
        "status": body.get("status"),
        "upstream": body.get("upstream"),
        "hub_healthy": body.get("hub_healthy"),
        "last_probe_at": body.get("last_probe_at"),
        "last_flip_at": body.get("last_flip_at"),
        "flips": body.get("flips"),
    }


async def local_snapshot(app) -> dict:
    """`GET /standby/local`: what a PEER needs to assess this instance."""
    from snowline_platform.db import session_scope

    block = await anyio.to_thread.run_sync(replication_health_from_scope, session_scope)
    registry = app.state.registry
    return {
        "instance": instance_id(),
        "train": platform_train(),
        "services": local_services(registry),
        "streams": local_streams(block, registry),
        "front": await fetch_front(),
    }


# --------------------------------------------------------------------------
# Peer discovery (this instance's own platform outbound subscriptions)
# --------------------------------------------------------------------------


def platform_url_of(target_url: str) -> str:
    """The peer's gateway base from an outbound ingest URL: the platform
    stream's `<platform>/replication/events/ingest`, or a plugin's
    `<platform>/via/<plugin>/…` (decision 0b8390f7)."""
    if VIA_SEGMENT in target_url:
        return target_url.split(VIA_SEGMENT, 1)[0].rstrip("/")
    if target_url.endswith(PLATFORM_INGEST_PATH):
        return target_url[: -len(PLATFORM_INGEST_PATH)].rstrip("/")
    parts = urlsplit(target_url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


def discover_peers(session) -> list[dict]:
    """[{peer, platform_url, self}] from the platform's ACTIVE live (non-
    backfill) outbound subscriptions. `self` is this instance's id as the
    stream's own `source_id` names it."""
    subs = session.execute(
        select(ReplicationSubscription).where(ReplicationSubscription.active.is_(True))
    ).scalars().all()
    peers: dict[str, dict] = {}
    for sub in subs:
        if is_backfill_source(sub.source_id):
            continue
        url = platform_url_of(sub.target_url)
        name = peer_instance(sub.peer_source_id) or urlsplit(url).hostname or url
        peers.setdefault(
            name,
            {"peer": name, "platform_url": url, "self": peer_instance(sub.source_id)},
        )
    return sorted(peers.values(), key=lambda p: p["peer"])


def _discover_from_scope() -> tuple[list[dict], str | None]:
    from snowline_platform.db import session_scope

    try:
        with session_scope() as session:
            return discover_peers(session), None
    except Exception as exc:  # noqa: BLE001 - /health must never fail on this
        return [], f"{type(exc).__name__}: {exc}"[:300]


# --------------------------------------------------------------------------
# The hub-side computation
# --------------------------------------------------------------------------


async def _get_json(client: httpx.AsyncClient, url: str) -> tuple[int | None, object, str | None]:
    """(status, json-or-None, transport error)."""
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        return None, None, type(exc).__name__
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body, None


async def fetch_peer(
    client: httpx.AsyncClient, platform_url: str, *, per_get: float = PEER_BUDGET_SECONDS * 0.8
) -> dict:
    """Read one peer's relay routes concurrently, EACH under its own
    `per_get` deadline (kept inside the caller's overall budget), so one
    hanging route (`/stack/report`) costs only its own field, never the
    answers that did land. reachable = its platform answered HTTP at all (a
    404 is an older peer, not an outage)."""
    results: dict[str, tuple] = {}

    async def one(key: str, path: str) -> None:
        results[key] = (None, None, f"no answer within {per_get:g}s")
        with anyio.move_on_after(per_get):
            results[key] = await _get_json(client, f"{platform_url}{path}")

    async with anyio.create_task_group() as tg:
        tg.start_soon(one, "local", LOCAL_PATH)
        tg.start_soon(one, "report", REPORT_PATH)
        tg.start_soon(one, "manifest", "/replication/manifest")
    statuses = [r[0] for r in results.values()]
    reachable = any(s is not None for s in statuses)
    errors = [r[2] for r in results.values() if r[2]]
    local = results["local"]
    report = results["report"]
    manifest = results["manifest"]
    return {
        "reachable": reachable,
        "error": None if reachable else (errors[0] if errors else "no answer"),
        "snapshot": local[1] if local[0] == 200 and isinstance(local[1], dict) else None,
        "reports": report[1] if report[0] == 200 and isinstance(report[1], dict) else None,
        "manifest": manifest[1] if manifest[0] == 200 and isinstance(manifest[1], dict) else None,
        "local_status": local[0],
    }


def _stream_out(row: dict, direction: str, threshold: float) -> dict:
    out = {
        "participant": row["participant"],
        "direction": direction,
        "peer_key": row.get("peer_key"),
        "pending": row.get("pending"),
        "oldest_pending_age_s": row.get("oldest_pending_age_s"),
        "last_success_at": row.get("last_success_at"),
        "unreachable_since": row.get("unreachable_since"),
        "consecutive_failures": row.get("consecutive_failures"),
        "version_hold": row.get("version_hold") or 0,
        "last_error": row.get("last_error"),
    }
    out["flag"] = stream_flag(out, threshold)
    return out


def assess_peer(
    *,
    peer: dict,
    fetched: dict | None,
    hub_streams: list[dict],
    hub_instance: str | None,
    hub_cut: str | None,
    autocut: dict | None,
    data_threshold: float,
    code_max: int,
) -> dict:
    """One peer's full readiness row (pure: no I/O)."""
    name = peer["peer"]
    platform_url = peer.get("platform_url")
    fetched = fetched or {"reachable": False, "error": "no platform stream to this peer"}
    snapshot = fetched.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else None
    manifest = fetched.get("manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    reports = fetched.get("reports")
    reports = reports if isinstance(reports, dict) else {}

    # A peer's JSON is untrusted shape: every field is type-checked before
    # use, so a malformed snapshot degrades to "unknown", never a raise.
    peer_train = None
    services = None
    if snapshot is not None:
        train = snapshot.get("train")
        if isinstance(train, dict) and isinstance(train.get("version"), str):
            peer_train = train["version"]
        elif isinstance(train, str):
            peer_train = train
        raw_services = snapshot.get("services")
        services = raw_services if isinstance(raw_services, dict) else None
    if peer_train is None and isinstance(manifest.get("train_version"), str):
        peer_train = manifest["train_version"]
    code = compare_trains(peer_train, hub_cut)
    code["services"] = services

    streams = [
        _stream_out(r, HUB_TO_PEER, data_threshold)
        for r in hub_streams
        if peer_instance(r.get("peer_key")) == name
        or (
            platform_url
            and peer_instance(r.get("peer_key")) is None
            and str(r.get("peer_key", "")).startswith(platform_url)
        )
    ]
    if snapshot is not None:
        raw_streams = snapshot.get("streams")
        for r in raw_streams if isinstance(raw_streams, list) else []:
            if not isinstance(r, dict) or "participant" not in r:
                continue
            inst = peer_instance(r.get("peer_key"))
            if inst is None or hub_instance is None or inst == hub_instance:
                streams.append(_stream_out(r, PEER_TO_HUB, data_threshold))

    snapshot_missing = None
    if fetched.get("local_status") in (401, 403):
        snapshot_missing = (
            f"peer refused {LOCAL_PATH} (HTTP {fetched['local_status']}): untrusted "
            "source IP — the peer's SNOWLINE_TRUSTED_CIDRS must admit this "
            "instance's tailnet address"
        )
    rollup, reasons = assess(
        reachable=bool(fetched.get("reachable")),
        code=code,
        streams=streams,
        snapshot_available=snapshot is not None,
        code_behind_max=code_max,
        error=fetched.get("error"),
        snapshot_missing=snapshot_missing,
    )
    sync = sync_summary(reports.get("sync") if isinstance(reports.get("sync"), dict) else None)
    front = snapshot.get("front") if snapshot is not None else None
    front = front if isinstance(front, dict) else None
    notes: list[str] = []
    if sync and sync.get("last_outcome") in _SYNC_TROUBLE:
        notes.append(
            f"last stack sync {sync['last_outcome']}"
            + (f": {sync['detail']}" if sync.get("detail") else "")
        )
    if isinstance(front, dict) and front.get("upstream") == "spoke":
        notes.append("the failover front is routing to the spoke")
    if code["flag"] == "ahead":
        notes.append(
            f"peer runs {peer_train}, newer than the hub's latest cut "
            f"{hub_cut} — the hub's cut record may be stale"
        )
    if peer_train == SOURCE:
        notes.append("peer runs from a source checkout — no train to compare")
    return {
        "peer": name,
        "platform_url": platform_url,
        "reachable": bool(fetched.get("reachable")),
        "error": fetched.get("error"),
        "code": code,
        "data": {"streams": streams},
        "sync": sync,
        "autocut": autocut,
        "front": front,
        "rollup": rollup,
        "reasons": reasons,
        "notes": notes,
    }


def _utc_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def compute(
    app,
    *,
    client: httpx.AsyncClient | None = None,
    budget: float = PEER_BUDGET_SECONDS,
    home: Path | None = None,
    block: dict | None = None,
) -> dict:
    """The full `standby` block for this instance (shape in the docstring).
    `block` is this platform's own replication health block when the caller
    (`/health`) already computed it — not re-queried then."""
    from snowline_platform.db import session_scope

    peers, discover_error = await anyio.to_thread.run_sync(_discover_from_scope)
    if block is None:
        block = await anyio.to_thread.run_sync(replication_health_from_scope, session_scope)
    hub_streams = local_streams(block, app.state.registry)
    own_reports = await anyio.to_thread.run_sync(read_reports, home)
    autocut = with_live_host(
        autocut_summary(own_reports.get("autocut")),
        getattr(app.state, "autocut_host", None),
    )
    running = platform_train()
    cut = hub_latest_cut(own_reports.get("autocut"), running=running)
    hub_instance = next((p["self"] for p in peers if p.get("self")), None) or instance_id()

    # Instances seen only in plugin streams (no platform stream → no URL).
    known = {p["peer"] for p in peers}
    for row in hub_streams:
        inst = peer_instance(row.get("peer_key"))
        if inst and inst not in known and inst != hub_instance:
            known.add(inst)
            peers.append({"peer": inst, "platform_url": None, "self": hub_instance})

    fetched: dict[str, dict] = {}
    targets = [p for p in peers if p.get("platform_url")]
    if targets:
        own = client is None
        client = client or httpx.AsyncClient(timeout=httpx.Timeout(budget), follow_redirects=True)
        try:
            with anyio.move_on_after(budget):
                async with anyio.create_task_group() as tg:
                    for p in targets:
                        async def _one(p=p) -> None:
                            fetched[p["peer"]] = await fetch_peer(
                                client, p["platform_url"], per_get=budget * 0.8
                            )

                        tg.start_soon(_one)
        finally:
            if own:
                await client.aclose()
        for p in targets:
            fetched.setdefault(
                p["peer"],
                {"reachable": False, "error": f"no answer within {budget:g}s"},
            )

    data_threshold = data_behind_after_seconds()
    code_max = code_behind_max_trains()
    rows = [
        assess_peer(
            peer=p,
            fetched=fetched.get(p["peer"]),
            hub_streams=hub_streams,
            hub_instance=hub_instance,
            hub_cut=cut["version"],
            autocut=autocut,
            data_threshold=data_threshold,
            code_max=code_max,
        )
        for p in peers
    ]
    reasons = [f"{r['peer']}: {why}" for r in rows for why in r["reasons"]]
    if not rows:
        reasons.append(
            "no paired standby peer"
            + (f" (outbound subscriptions unreadable: {discover_error})" if discover_error else "")
        )
    return {
        "computed_at": _utc_iso(),
        "instance": hub_instance,
        "hub": {"train": running, "latest_cut": cut, "autocut": autocut},
        "thresholds": {
            "data_behind_after_s": data_threshold,
            "code_behind_max_trains": code_max,
        },
        "peers": rows,
        "rollup": worst([r["rollup"] for r in rows]),
        "reasons": reasons,
    }


def _failure_block(exc: BaseException) -> dict:
    return {
        "computed_at": _utc_iso(),
        "rollup": UNKNOWN,
        "reasons": [f"standby computation failed: {type(exc).__name__}: {exc}"[:300]],
        "peers": [],
    }


PLACEHOLDER_REASON = "standby readiness is being computed — ask again in a few seconds"


def _placeholder() -> dict:
    return {
        "rollup": UNKNOWN,
        "reasons": [PLACEHOLDER_REASON],
        "peers": [],
        "pending": True,
        "age_s": None,
    }


async def refresh(app, **kwargs) -> dict:
    """Compute the block and cache it for `CACHE_SECONDS` — a FAILURE too (as
    an UNKNOWN block naming the error), so a raising computation is retried
    once per window, not on every caller. Never raises."""
    state = app.state
    # TEST SEAM: tests inject an httpx client (MockTransport) here; production
    # never sets it, so compute() builds and closes its own per refresh.
    client = getattr(state, "standby_http_client", None)
    if client is not None:
        kwargs.setdefault("client", client)
    try:
        with anyio.fail_after(REFRESH_DEADLINE_SECONDS):
            result = await compute(app, **kwargs)
    except TimeoutError:
        log.warning("standby: refresh exceeded %ss", REFRESH_DEADLINE_SECONDS)
        result = {
            "computed_at": _utc_iso(),
            "rollup": UNKNOWN,
            "reasons": [
                f"standby refresh timed out (> {REFRESH_DEADLINE_SECONDS:g}s)"
            ],
            "peers": [],
        }
    except Exception as exc:  # liveness must not depend on this (logged with traceback)
        log.warning("standby: computation failed", exc_info=True)
        result = _failure_block(exc)
    state.standby_cache = (time.monotonic(), result)
    return result


def _cache(app) -> tuple[float, dict] | None:
    return getattr(app.state, "standby_cache", None)


def _served(hit: tuple[float, dict]) -> dict:
    """The cached block as served: plus `age_s`, seconds since it was
    computed, so a reader (the dashboard panel) can show staleness."""
    return {**hit[1], "age_s": round(max(time.monotonic() - hit[0], 0.0), 1)}


def _is_fresh(hit) -> bool:
    return hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS


def schedule_refresh(app, **kwargs) -> asyncio.Task | None:
    """Start ONE background refresh unless one is already in flight. The
    task is held on `app.state` (a strong reference — an unreferenced task
    can be garbage-collected mid-flight)."""
    state = app.state
    task = getattr(state, "standby_task", None)
    if task is not None and not task.done():
        return task
    task = asyncio.get_running_loop().create_task(refresh(app, **kwargs))
    state.standby_task = task
    return task


async def for_health(app, *, block: dict | None = None) -> dict:
    """The `health.standby` field — STALE-WHILE-REVALIDATE: answers at once
    with the last computed block (or an UNKNOWN placeholder before the first
    one lands) and, when that is older than `CACHE_SECONDS`, schedules the
    refresh in the background. `/health` therefore never awaits a peer, a
    DB-heavy computation, or a lock: the failover front's 2 s probe of the
    hub's `/health` can't be tripped by a sleeping standby. Never raises."""
    hit = _cache(app)
    if not _is_fresh(hit):
        try:
            schedule_refresh(app, block=block)
        except Exception:  # scheduling must not fail /health (logged with traceback)
            log.warning("standby: could not schedule a refresh", exc_info=True)
    return _served(hit) if hit is not None else _placeholder()


async def for_route(app) -> dict:
    """`GET /standby`: the same cache, but before the FIRST block exists it
    waits (bounded) for the in-flight refresh, so the dashboard's first load
    shows real data rather than the placeholder. Not used by `/health`."""
    result = await for_health(app)
    if _cache(app) is None:
        task = getattr(app.state, "standby_task", None)
        if task is not None:
            with anyio.move_on_after(PEER_BUDGET_SECONDS + 1.0):
                await asyncio.shield(task)
        hit = _cache(app)
        if hit is not None:
            return _served(hit)
    return result


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

router = APIRouter()


@router.get("/standby")
async def standby(request: Request) -> dict:
    """The readiness block (behind the trust middleware, like `/plugins`)."""
    return await for_route(request.app)


@router.get(LOCAL_PATH)
async def standby_local(request: Request) -> dict:
    """This instance's train + outbox streams, for a PEER's readiness view.
    Tailnet-gated with the replication routes' own CIDR check (defense in
    depth beside the trust middleware); read-only, no secrets."""
    _require_trusted(request)
    return await local_snapshot(request.app)


@router.get(REPORT_PATH)
async def stack_report(request: Request) -> dict:
    """The sync / bootstrap / autocut reports on this machine's disk, redacted
    (`redact`), read-only — relayed to the peer's readiness view."""
    _require_trusted(request)
    return await anyio.to_thread.run_sync(read_reports, None)
