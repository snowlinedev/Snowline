"""The governance `main` MCP surface — the decision + artifact tools.

Mirrors the frozen monolith's FastMCP registration pattern (`mcp_server.py`, the
`core_mcp` surface is the closest analog): a `FastMCP` instance with the
governance tools registered on it, served over streamable HTTP, mounted on the
governance app at `/mcp`. This increment carries the 5 decision tools —
`record_decision`, `supersede_decision`, `get_decision`, `list_decisions`,
`applicable_decisions` — plus the artifact tools (`register_artifact`,
`revise_artifact`, `resolve_artifact`, `get_artifact`, `list_artifacts`,
`set_governs`, `set_maturity`, `applicable_artifacts`). The separate `shadow`
surface lands in a later increment.

Each tool runs its blocking DB work in a thread (the monolith's
`anyio.to_thread.run_sync` pattern) so the async transport isn't blocked. The
scope dependency is resolved through an injectable `ScopeClient` —
`build_main_surface(scope_client=...)` lets a test pass a stub; production builds
the real `HttpScopeClient`. The artifact governs tools resolve each scope slug →
`(id, slug)` against the platform BEFORE the DB write (the soft-reference pattern
the decision tools use), so a governs edge keys on the STABLE `scope_id`.
"""

from __future__ import annotations

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from snowline_governance import artifacts, decisions
from snowline_governance.db import session_scope
from snowline_governance.scope_client import (
    HttpScopeClient,
    ScopeClient,
    ScopeNotFoundError,
)

_MAIN_INSTRUCTIONS = """\
This is the Snowline GOVERNANCE surface — the real decision graph AND the \
artifact (spec/plan/reference) graph: record/supersede decisions and read them, \
register/revise/resolve governing artifacts and set their governs/maturity, and \
read the ancestor-inherited governance that applies at a scope. Decisions: \
`record_decision`, `supersede_decision`, `get_decision`, `list_decisions`, \
`applicable_decisions`. Artifacts: `register_artifact` (inline-backed — content \
lives in the substrate), `revise_artifact`, `resolve_artifact` (collapse \
competing version leaves), `get_artifact`, `list_artifacts`, `set_governs`, \
`set_maturity`, `applicable_artifacts` (artifacts governing a scope, \
ancestor-inherited). `applicable_*` resolve "what governs here" by walking the \
scope tree UPWARD and halting at the first isolated ancestor. Scopes are owned \
by the platform; governance references them by slug and reads the scope tree \
from the platform to compute applicability.\
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

    # --- artifact tools -----------------------------------------------------

    def _resolve_governs(governs) -> dict[str, dict]:
        """Resolve every scope slug in a `governs` argument against the platform,
        returning slug → the platform's scope row (carrying the stable `id` the
        soft governs edge keys on). `'*'`/`None` need no resolution. Raises
        `ScopeNotFoundError` for an unknown slug (the write is rejected before any
        DB mutation), mirroring the decision tools' resolve-first contract."""
        slugs: list[str] = []
        if isinstance(governs, str):
            if governs != "*":
                slugs = [governs]
        elif isinstance(governs, (list, tuple)):
            slugs = [s for s in governs if isinstance(s, str) and s != "*"]
        resolved: dict[str, dict] = {}
        for slug in slugs:
            sc = client.resolve(slug)
            if sc is None:
                raise ScopeNotFoundError(
                    f"no scope with slug {slug!r} — register it on the platform "
                    "first"
                )
            resolved[slug] = sc
        return resolved

    def _register_artifact_sync(
        body, doc_kind, maturity, governs, backend
    ):
        resolved = _resolve_governs(governs)
        with session_scope() as session:
            return artifacts.register_artifact(
                session,
                body=body,
                doc_kind=doc_kind,
                maturity=maturity,
                governs=governs,
                backend=backend,
                resolved_scopes=resolved,
            )

    @mcp.tool()
    async def register_artifact(
        body: str,
        doc_kind: str = "spec",
        maturity: str = "draft",
        governs=None,
        backend: str = "inline",
    ) -> dict:
        """Register an INLINE governing artifact (a spec/plan/reference doc) —
        its content (`body`) lives in the substrate, versioned by governance.
        `doc_kind` ∈ spec/plan/reference; `maturity` ∈ draft/exploratory/stable.
        `governs` accepts a scope slug, a list of slugs, `'*'` (all scopes), or
        None (ungoverned) — each slug is resolved against the platform first.
        Only `backend='inline'` is supported; `'git'` is rejected (it needs a
        repo registry that's a GitHub-plugin concern). Always mints a fresh
        artifact + an initial version."""
        return await anyio.to_thread.run_sync(
            _register_artifact_sync, body, doc_kind, maturity, governs, backend
        )

    def _revise_artifact_sync(
        artifact_id, relation, supersedes, body_snapshot, summary
    ):
        with session_scope() as session:
            return artifacts.revise_artifact(
                session,
                artifact_id,
                relation=relation,
                supersedes=supersedes,
                body_snapshot=body_snapshot,
                summary=summary,
            )

    @mcp.tool()
    async def revise_artifact(
        artifact_id: str,
        relation: str = "refines",
        supersedes: str | None = None,
        body_snapshot: str | None = None,
        summary: str | None = None,
    ) -> dict:
        """Create a new version of an artifact. `relation` is `refines` (normal
        improve) or `pivot` (a redirection kept as a branch). `supersedes` is the
        version id the new one follows (defaults to the current leaf); superseding
        a NON-leaf creates a competing branch. `body_snapshot` is the new inline
        content; `summary` a one-line "why this version"."""
        return await anyio.to_thread.run_sync(
            _revise_artifact_sync,
            artifact_id, relation, supersedes, body_snapshot, summary,
        )

    def _resolve_artifact_sync(artifact_id, version_id):
        with session_scope() as session:
            return artifacts.resolve_artifact(session, artifact_id, version_id)

    @mcp.tool()
    async def resolve_artifact(artifact_id: str, version_id: str) -> dict:
        """Collapse an artifact's competing version leaves. `version_id` is the
        LOSING leaf — it flips to `status=superseded` (staying in the audit trail)
        and the remaining leaf becomes canonical. Requires the artifact to have >1
        current leaf and `version_id` to be one of them."""
        return await anyio.to_thread.run_sync(
            _resolve_artifact_sync, artifact_id, version_id
        )

    def _get_artifact_sync(artifact_id):
        with session_scope() as session:
            return artifacts.get_artifact(session, artifact_id)

    @mcp.tool()
    async def get_artifact(artifact_id: str) -> dict:
        """Read one artifact's full record by id — identity, doc_kind, maturity,
        governs, the current leaf + all competing leaves + branch points. The
        on-demand expansion of a `list_artifacts` header. Read-only."""
        return await anyio.to_thread.run_sync(_get_artifact_sync, artifact_id)

    def _list_artifacts_sync(governs, limit):
        governs_scope_id = None
        if governs is not None:
            sc = client.resolve(governs)
            governs_scope_id = sc["id"] if sc is not None else None
        with session_scope() as session:
            return artifacts.list_artifacts(
                session,
                governs=governs,
                governs_scope_id=governs_scope_id,
                limit=limit,
            )

    @mcp.tool()
    async def list_artifacts(
        governs: str | None = None, limit: int | None = None
    ) -> dict:
        """List registered artifacts as compact rows (id, doc_kind, backend,
        maturity, governs, version_count, is_branched), newest-first, capped.
        `governs` (a scope slug, resolved against the platform) narrows to
        artifacts governing that scope — by an association row OR `governs_all`.
        Expand any row via `get_artifact(id)`. Read-only."""
        return await anyio.to_thread.run_sync(
            _list_artifacts_sync, governs, limit
        )

    def _set_governs_sync(artifact_id, governs):
        resolved = _resolve_governs(governs)
        with session_scope() as session:
            return artifacts.set_governs(
                session, artifact_id, governs, resolved_scopes=resolved
            )

    @mcp.tool()
    async def set_governs(artifact_id: str, governs=None) -> dict:
        """Set (or clear) an artifact's `governs` after registration. `governs`
        accepts a scope slug, a list of slugs, `'*'` (all scopes), or None (clear
        both). Each slug is resolved against the platform first; the new set
        fully REPLACES the old. Returns the refreshed artifact."""
        return await anyio.to_thread.run_sync(
            _set_governs_sync, artifact_id, governs
        )

    def _set_maturity_sync(artifact_id, maturity):
        with session_scope() as session:
            return artifacts.set_maturity(session, artifact_id, maturity)

    @mcp.tool()
    async def set_maturity(artifact_id: str, maturity: str) -> dict:
        """Set an artifact's `maturity` — draft → exploratory → stable. A
        descriptor, not a gate: any direction is allowed and no version is
        created. Returns the refreshed artifact."""
        return await anyio.to_thread.run_sync(
            _set_maturity_sync, artifact_id, maturity
        )

    def _applicable_artifacts_sync(scope, limit):
        with session_scope() as session:
            return artifacts.applicable_artifacts(
                session, scope, client, limit=limit
            )

    @mcp.tool()
    async def applicable_artifacts(scope: str, limit: int = 50) -> dict:
        """Artifacts APPLICABLE at a scope, ANCESTOR-INHERITED — the governing
        docs a reader at `scope` inherits, not just its own. Reads the scope tree
        from the platform, walking UPWARD and HALTING at the first isolated
        ancestor + the forest root; each inherited row carries `from_scope` (the
        ancestor slug it matched), and a `governs_all` artifact carries
        `from_scope='*'`. The artifact analog of `applicable_decisions`."""
        return await anyio.to_thread.run_sync(
            _applicable_artifacts_sync, scope, limit
        )

    return mcp
