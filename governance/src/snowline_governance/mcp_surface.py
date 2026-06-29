"""The governance `main` MCP surface — the decision tools.

Mirrors the frozen monolith's FastMCP registration pattern (`mcp_server.py`, the
`core_mcp` surface is the closest analog): a `FastMCP` instance with the decision
tools registered on it, served over streamable HTTP, mounted on the governance
app at `/mcp`. This increment carries the 5 decision tools — `record_decision`,
`supersede_decision`, `get_decision`, `list_decisions`, `applicable_decisions`.
The artifact tools + the separate `shadow` surface land in later increments.

Each tool runs its blocking DB work in a thread (the monolith's
`anyio.to_thread.run_sync` pattern) so the async transport isn't blocked. The
scope dependency (`applicable_decisions` / `record_decision`) is resolved through
an injectable `ScopeClient` — `build_main_surface(scope_client=...)` lets a test
pass a stub; production builds the real `HttpScopeClient`.
"""

from __future__ import annotations

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from snowline_governance import decisions
from snowline_governance.db import session_scope
from snowline_governance.scope_client import (
    HttpScopeClient,
    ScopeClient,
    ScopeNotFoundError,
)

_MAIN_INSTRUCTIONS = """\
This is the Snowline GOVERNANCE surface — the real decision graph: record/\
supersede decisions and read them, including the ancestor-inherited governance \
that applies at a scope. It carries `record_decision`, `supersede_decision`, \
`get_decision`, `list_decisions`, and `applicable_decisions` (decisions \
APPLICABLE at a scope, ancestor-inherited — how you resolve "what governs here", \
walking the scope tree UPWARD and halting at the first isolated ancestor). \
Scopes are owned by the platform; governance references them by slug and reads \
the scope tree from the platform to compute applicability.\
"""

# DNS-rebinding protection on the streamable-HTTP transport (the monolith's
# default). Hosts/origins can be widened via env in a later increment if needed;
# behind the platform trust gate the plugin is reached on the tailnet.
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def build_main_surface(scope_client: ScopeClient | None = None) -> FastMCP:
    """Build the `main` FastMCP surface with the decision tools registered.

    `scope_client` is injectable (tests pass a stub); defaults to the real
    `HttpScopeClient` talking to the platform's scope API. Returns a fresh
    `FastMCP` so the app builds one and the tests can build their own.
    """
    client: ScopeClient = scope_client or HttpScopeClient()

    mcp = FastMCP(
        "snowline-governance",
        stateless_http=True,
        transport_security=_SECURITY,
        instructions=_MAIN_INSTRUCTIONS,
    )

    def _record_decision_sync(scope: str, decision: str, rationale: str | None):
        sc = client.resolve(scope)
        if sc is None:
            raise ScopeNotFoundError(
                f"no scope with slug {scope!r} — register it on the platform first"
            )
        with session_scope() as session:
            return decisions.record_decision(
                session,
                scope_slug=sc["slug"],
                scope_id=sc["id"],
                decision=decision,
                rationale=rationale,
            )

    @mcp.tool()
    async def record_decision(
        scope: str, decision: str, rationale: str | None = None
    ) -> dict:
        """Record a design/planning decision against a scope (a Snowline slug,
        `owner/repo` or an initiative/component slug). The scope is resolved
        against the platform; the decision references it by a soft reference.
        Always creates a fresh leaf — to OVERRIDE an existing decision use
        `supersede_decision` so the two are linked.
        """
        return await anyio.to_thread.run_sync(
            _record_decision_sync, scope, decision, rationale
        )

    def _supersede_decision_sync(
        prior_decision_id: str,
        decision: str,
        rationale: str | None,
        scope: str | None,
    ):
        with session_scope() as session:
            return decisions.supersede_decision(
                session, prior_decision_id, decision, rationale, scope
            )

    @mcp.tool()
    async def supersede_decision(
        prior_decision_id: str,
        decision: str,
        rationale: str | None = None,
        scope: str | None = None,
    ) -> dict:
        """Record a new decision that SUPERSEDES an existing one (linking them in
        the supersession DAG). The prior row stays for audit; reads return only
        the new leaf by default. Supersession is intra-scope: `scope` defaults to
        the prior's and passing a different one raises.
        """
        return await anyio.to_thread.run_sync(
            _supersede_decision_sync, prior_decision_id, decision, rationale, scope
        )

    def _get_decision_sync(decision_id: str):
        with session_scope() as session:
            return decisions.get_decision(session, decision_id)

    @mcp.tool()
    async def get_decision(decision_id: str) -> dict:
        """Read one decision's FULL body — statement + rationale + lineage
        (`supersedes`/`superseded_by`) — by id. The on-demand expansion of a
        header echoed in the list/applicable read surfaces. Read-only.
        """
        return await anyio.to_thread.run_sync(_get_decision_sync, decision_id)

    def _list_decisions_sync(
        scope: str, limit: int | None, include_superseded: bool
    ):
        sc = client.resolve(scope)
        if sc is None:
            raise ScopeNotFoundError(
                f"no scope with slug {scope!r} — register it on the platform first"
            )
        with session_scope() as session:
            return decisions.list_decisions(
                session,
                scope_id=sc["id"],
                scope_slug=sc["slug"],
                limit=limit,
                include_superseded=include_superseded,
            )

    @mcp.tool()
    async def list_decisions(
        scope: str, limit: int | None = None, include_superseded: bool = False
    ) -> dict:
        """Browse a scope's EXACT-scope decision history — headers (id, one-line
        summary, recorded `at`, supersedes/superseded_by lineage), newest-first,
        capped. By default returns only current leaves; `include_superseded=True`
        exposes full chains for audit. Expand any row via `get_decision(id)`.
        """
        return await anyio.to_thread.run_sync(
            _list_decisions_sync, scope, limit, include_superseded
        )

    def _applicable_decisions_sync(
        scope: str, include_superseded: bool, limit: int
    ):
        with session_scope() as session:
            return decisions.applicable_decisions(
                session,
                scope,
                client,
                include_superseded=include_superseded,
                limit=limit,
            )

    @mcp.tool()
    async def applicable_decisions(
        scope: str, include_superseded: bool = False, limit: int = 50
    ) -> dict:
        """Decisions APPLICABLE at a scope, ANCESTOR-INHERITED — the full
        governance a reader at `scope` inherits, not just its own. Reads the scope
        tree from the platform, walking UPWARD and HALTING at the first isolated
        ancestor + the forest root; each inherited row carries `from_scope` (the
        ancestor slug it came from), absent on the scope's OWN decisions. This is
        the UPWARD applicability view (vs `list_decisions`, exact-scope).
        """
        return await anyio.to_thread.run_sync(
            _applicable_decisions_sync, scope, include_superseded, limit
        )

    return mcp
