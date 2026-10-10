"""The pre-cut milestone gate (issue #242).

`snowline release cut` asks pm's `milestone_status` about the train's release
milestone and REFUSES (without --force) while required items remain open or
the milestone is not achievable. The gate is advisory when it cannot judge:
an unresolved milestone (`registry: null`) or an unreachable pm warns loudly
and proceeds — a pm outage must never block a cut.

pm is reached through the platform's own gateway main surface
(`<SNOWLINE_PLATFORM_URL>/mcp`, tool `pm__milestone_status`) with the mcp
client the gateway already uses, so the dependency on pm stays soft.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .model import ReleaseError

MILESTONE_ORG = "snowlinedev"
MAX_LISTED = 10
_TRAIN_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

# milestone ref -> pm `milestone_status` payload. Raises GateUnavailable when
# pm cannot be reached or answers unusably.
StatusFetcher = Callable[[str], dict]


class GateUnavailable(Exception):
    """pm could not be asked (outage, auth, bad payload)."""


def train_milestone(version: str) -> str:
    """Train `vX.Y.Z` -> registry milestone `snowlinedev/vX.Y`.

    A PATCH respin belongs to its MINOR's milestone: v0.5.0 and v0.5.1 are both
    `snowlinedev/v0.5`; v1.0.0 -> `snowlinedev/v1.0`."""
    match = _TRAIN_RE.match(version)
    if not match:
        raise ReleaseError(f"cannot map train version {version!r} to a milestone")
    return f"{MILESTONE_ORG}/v{match.group(1)}.{match.group(2)}"


@dataclass
class GateVerdict:
    milestone: str
    status: str  # "ok" | "blocked" | "skipped"
    reason: str = ""
    required_remaining: int = 0
    lines: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    def record(self, forced: bool) -> dict:
        """The additive `gated` field for release/train.json."""
        if self.status == "skipped":
            return {"skipped": self.reason, "milestone": self.milestone}
        return {
            "milestone": self.milestone,
            "forced": bool(forced and self.blocked),
            "required_remaining": self.required_remaining,
        }


def evaluate(milestone: str, fetch: StatusFetcher) -> GateVerdict:
    try:
        status = fetch(milestone)
    except Exception as exc:  # advisory: nothing here may block a cut
        return GateVerdict(milestone, "skipped", f"pm unreachable: {exc}")
    if not isinstance(status, dict) or status.get("registry") is None:
        return GateVerdict(milestone, "skipped", "milestone does not resolve in the registry")

    completion = status.get("completion") or {}
    req = completion.get("required_remaining") or {}
    count = int(req.get("count") or 0)
    items = req.get("items") or []
    achievable = completion.get("achievable", True)
    blockers = completion.get("blockers") or {}
    cancelled = blockers.get("blocked_by_cancelled") or []
    stale = blockers.get("stale_criteria") or []

    lines: list[str] = []
    if count:
        lines.append(f"{count} required item(s) still open:")
        for it in items[:MAX_LISTED]:
            lines.append(f"  - {str(it.get('id', '?'))[:8]}  {it.get('title', '')}")
        if count > MAX_LISTED:
            lines.append(f"  ... and {count - MAX_LISTED} more")
    if cancelled:
        lines.append(f"blocked_by_cancelled: {_fmt(cancelled)}")
    if stale:
        lines.append(f"stale criteria: {_fmt(stale)}")
    if achievable is False and not count:
        lines.append("milestone is not achievable (completion.achievable is false)")
    summary = status.get("readiness_summary")
    if summary:
        lines.append(f"readiness: {_fmt(summary)}")

    blocked = count > 0 or achievable is False
    return GateVerdict(
        milestone, "blocked" if blocked else "ok",
        required_remaining=count, lines=lines,
    )


def _fmt(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def fetch_milestone_status(milestone: str) -> dict:
    """Production fetcher: MCP `tools/call pm__milestone_status` via the gateway."""
    import anyio

    from snowline_platform import config

    url = config.platform_self_url() + "/mcp"

    async def _call() -> dict:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=True) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "pm__milestone_status", {"milestone": milestone}
                    )
        if result.isError:
            raise GateUnavailable(f"pm__milestone_status errored: {result.content!r}")
        sc = getattr(result, "structuredContent", None)
        if sc:
            return sc.get("result", sc) if isinstance(sc, dict) else sc
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                return json.loads(text)
        raise GateUnavailable("empty milestone_status response")

    try:
        return anyio.run(_call)
    except GateUnavailable:
        raise
    except BaseException as exc:  # connection refused, ExceptionGroup, bad JSON
        raise GateUnavailable(f"{url}: {exc!r}") from exc
