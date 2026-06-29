"""The governance MCP surfaces — the `main` (real-write) surface and the `shadow`
(speculation) surface.

Mirrors the frozen monolith's FastMCP registration pattern (`mcp_server.py`):

  - `build_main_surface` — the REAL-WRITE governance surface, mounted at `/mcp`.
    The 5 decision tools (`record_decision`, `supersede_decision`, `get_decision`,
    `list_decisions`, `applicable_decisions`) + the artifact tools
    (`register_artifact`, `revise_artifact`, `resolve_artifact`, `get_artifact`,
    `list_artifacts`, `set_governs`, `set_maturity`, `applicable_artifacts`).

  - `build_shadow_surface` — the SPECULATION surface, mounted at `/shadow/mcp`.
    The 8 shadow-WRITE tools (`create_branch`, `list_branches`, `get_branch`,
    `set_narrative_notes`, `add_node`, `add_citation`, `list_citations`,
    `shadow_corpus_search`) PLUS the read-real grounding tools (the decision +
    artifact READS) so a speculation agent can ground in the real graph — and NO
    real-write verb. That absence IS the isolation guarantee (decision 8a7f0a11,
    mirroring the monolith's `/shadow/mcp`): a speculation session physically
    cannot mutate the real graph, because `record_decision` / `supersede_decision`
    / the artifact writes are not registered on this surface.

The read-real grounding half is shared by REGISTERING the same handlers on both
surfaces (`_register_read_tools` — one source of truth per tool, the monolith's
re-registration pattern), so the two surfaces can't drift on what a "read" means.

Each tool runs its blocking DB work in a thread (the monolith's
`anyio.to_thread.run_sync` pattern) so the async transport isn't blocked. The
scope dependency is resolved through an injectable `ScopeClient` —
`build_*_surface(scope_client=...)` lets a test pass a stub; production builds the
real `HttpScopeClient`. The governs/scope-bearing tools resolve each scope slug →
`(id, slug)` against the platform BEFORE the DB write (the soft-reference pattern),
so a stored edge keys on the STABLE `scope_id`.
"""

from __future__ import annotations

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from snowline_governance import artifacts, decisions, graduation, shadow
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

_SHADOW_INSTRUCTIONS = """\
This is the Snowline SPECULATION (shadow) surface (decision 8a7f0a11): a place to \
explore rival design directions in ISOLATION from the real governance graph, \
until a line is explicitly graduated. You hold WRITE-SHADOW + READ-REAL tools \
only. Write-shadow: `create_branch` (a named speculation line per scope, \
addressed `<scope>:<name>`), `list_branches`, `get_branch`, \
`set_narrative_notes` (the running reasoning thread), `add_node` (a \
not-yet-real decision), `add_citation` (inward-only: a node may cite another \
node in its OWN branch, or a real decision — never the reverse), \
`list_citations`, and `shadow_corpus_search` (full-text over the shadow content \
+ the real decisions backlinked to a shadow line). Read-real grounding: \
`get_decision`, `list_decisions`, `applicable_decisions`, `get_artifact`, \
`list_artifacts`, `applicable_artifacts` — read the real graph freely to ground \
your speculation. It deliberately exposes NO real-write verb — `record_decision`, \
`supersede_decision`, and the artifact write verbs are ABSENT by construction, on \
the separate `main` surface only. This ensures a speculation session physically \
cannot misfire a real decision into the graph (the write-isolation safety \
property). Scopes are owned by the platform; governance references them by slug.\
"""

# DNS-rebinding protection on the streamable-HTTP transport (the monolith's
# default). Hosts/origins can be widened via env in a later increment if needed;
# behind the platform trust gate the plugin is reached on the tailnet.
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _register_read_tools(mcp: FastMCP, client: ScopeClient) -> None:
    """Register the READ-REAL governance tools on `mcp` — the decision reads
    (`get_decision`, `list_decisions`, `applicable_decisions`) + the artifact
    reads (`get_artifact`, `list_artifacts`, `applicable_artifacts`). Shared by
    BOTH the `main` surface (its read half) and the `shadow` surface (its
    read-real grounding half), so the two surfaces register the SAME handlers —
    one source of truth per tool. Pure-read: no write verb is registered here, so
    a surface that wants only grounding (shadow) gets exactly that."""

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

    # --- graduation (shadow → real) — a REAL write, MAIN surface only --------
    # The ONE explicit crossing from shadow into the real graph (decision
    # 99b92e1d "the principal split"): the shadow agent on `/shadow/mcp` drafts /
    # proposes, the ratifying principal HERE on `main` executes. It mints a real
    # `record_decision`, so it lives with the real-write verbs and is ABSENT from
    # the shadow surface — surface placement IS the principal split.

    def _graduate_sync(
        node_id: str,
        dest_scope: str | None,
        decision: str | None,
        rationale: str | None,
        promote_closure: bool,
    ):
        # Default to the node's cradle scope (resolved inside the service from the
        # branch's soft scope ref); an explicit `dest_scope` is resolved against
        # the platform first (the soft-reference contract) before any DB write.
        dest_slug = None
        dest_id = None
        if dest_scope is not None:
            sc = client.resolve(dest_scope)
            if sc is None:
                raise ScopeNotFoundError(
                    f"no scope with slug {dest_scope!r} — register it on the "
                    "platform first"
                )
            dest_slug, dest_id = sc["slug"], sc["id"]
        with session_scope() as session:
            return graduation.graduate_node(
                session,
                node_id,
                dest_scope_slug=dest_slug,
                dest_scope_id=dest_id,
                decision=decision,
                rationale=rationale,
                promote_closure=promote_closure,
            )

    @mcp.tool()
    async def graduate(
        node_id: str,
        dest_scope: str | None = None,
        decision: str | None = None,
        rationale: str | None = None,
        promote_closure: bool = True,
    ) -> dict:
        """GRADUATE a shadow node into a REAL decision — the one explicit crossing
        from speculation into the real graph (the human-ratified write the shadow
        agent does NOT hold). Translates the node's `statement → decision` /
        `rationale → rationale` (override either with a ratified edit via
        `decision`/`rationale`), records it, and stamps bidirectional provenance
        (the decision's shadow origin + the node's `graduated_decision_id`).

        `dest_scope` (a slug, resolved against the platform) chooses the scope; it
        defaults to the node's CRADLE scope (its branch's anchor). Idempotent:
        re-graduating an already-graduated node returns the existing decision.
        `promote_closure` (default true) graduates the node's un-graduated
        shadow-cited ancestors first, in dependency order, so the referenced
        reasoning comes along."""
        return await anyio.to_thread.run_sync(
            _graduate_sync,
            node_id, dest_scope, decision, rationale, promote_closure,
        )

    # --- decision + artifact READ tools (shared with the shadow surface) ----
    # The read-real grounding set; the same handlers register on `shadow`.
    _register_read_tools(mcp, client)

    # --- artifact WRITE tools (main surface only — ABSENT from shadow) -------

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

    return mcp


def build_shadow_surface(scope_client: ScopeClient | None = None) -> FastMCP:
    """Build the `shadow` FastMCP surface — the SPECULATION surface (decision
    8a7f0a11), mounted on the governance app at `/shadow/mcp`.

    A SECOND FastMCP instance, mirroring the monolith's `/shadow` + `/core/shadow`:
    the 8 shadow-WRITE tools (`create_branch`, `list_branches`, `get_branch`,
    `set_narrative_notes`, `add_node`, `add_citation`, `list_citations`,
    `shadow_corpus_search`) PLUS the read-real grounding tools (the SAME decision
    + artifact READ handlers the `main` surface registers, via
    `_register_read_tools`) so a speculation agent can ground in the real graph.

    NO real-write verb is registered here — `record_decision`, `supersede_decision`,
    `register_artifact`, `revise_artifact`, `resolve_artifact`, `set_governs`,
    `set_maturity` are ABSENT by construction. That absence IS the isolation: a
    speculation session connecting to `/shadow/mcp` physically cannot mutate the
    real graph.

    `scope_client` is injectable (tests pass a stub); defaults to the real
    `HttpScopeClient`. The scope-bearing shadow tools resolve the slug →
    `(id, slug)` against the platform BEFORE the DB write (the soft-reference
    pattern), so a branch anchors on the STABLE `scope_id`.
    """
    client: ScopeClient = scope_client or HttpScopeClient()

    mcp = FastMCP(
        "snowline-governance-shadow",
        stateless_http=True,
        transport_security=_SECURITY,
        instructions=_SHADOW_INSTRUCTIONS,
    )

    def _resolve_scope(scope: str) -> dict:
        sc = client.resolve(scope)
        if sc is None:
            raise ScopeNotFoundError(
                f"no scope with slug {scope!r} — register it on the platform first"
            )
        return sc

    def _create_branch_sync(scope, name, narrative_notes):
        sc = _resolve_scope(scope)
        with session_scope() as session:
            return shadow.create_branch(
                session, sc["slug"], sc["id"], name, narrative_notes
            )

    @mcp.tool()
    async def create_branch(
        scope: str, name: str, narrative_notes: str | None = None
    ) -> dict:
        """Open a new speculative branch in `scope` (a Snowline slug, resolved
        against the platform), addressed `<scope>:<name>` (name unique within the
        scope). Optionally seed its narrative-notes doc. Shadow-only: invisible to
        the real graph, and nothing real may cite it."""
        return await anyio.to_thread.run_sync(
            _create_branch_sync, scope, name, narrative_notes
        )

    def _list_branches_sync(scope, include_done):
        sc = _resolve_scope(scope)
        with session_scope() as session:
            return {
                "branches": shadow.list_branches(
                    session, sc["slug"], sc["id"], include_done
                )
            }

    @mcp.tool()
    async def list_branches(scope: str, include_done: bool = False) -> dict:
        """List speculative branches in `scope`, newest first. Archived branches
        are hidden by default — they represent concluded speculation. Pass
        `include_done=True` to reveal them."""
        return await anyio.to_thread.run_sync(
            _list_branches_sync, scope, include_done
        )

    def _get_branch_sync(scope, name):
        with session_scope() as session:
            return shadow.get_branch(session, scope, name)

    @mcp.tool()
    async def get_branch(scope: str, name: str) -> dict:
        """A branch with its nodes (the verdicts) and narrative notes (the
        reasoning) — the re-entry surface for a speculation line."""
        return await anyio.to_thread.run_sync(_get_branch_sync, scope, name)

    def _set_narrative_notes_sync(scope, name, narrative_notes):
        with session_scope() as session:
            return shadow.set_narrative_notes(
                session, scope, name, narrative_notes
            )

    @mcp.tool()
    async def set_narrative_notes(
        scope: str, name: str, narrative_notes: str | None
    ) -> dict:
        """Replace a branch's narrative-notes doc — the running thread that
        reconstructs the reasoning on re-entry."""
        return await anyio.to_thread.run_sync(
            _set_narrative_notes_sync, scope, name, narrative_notes
        )

    def _add_node_sync(scope, name, statement, rationale):
        with session_scope() as session:
            return shadow.add_node(session, scope, name, statement, rationale)

    @mcp.tool()
    async def add_node(
        scope: str, name: str, statement: str, rationale: str | None = None
    ) -> dict:
        """Add a speculative-decision node to a branch: `statement` is the
        not-yet-real decision, `rationale` its crisp why. Individually addressable
        so a future graduation can cherry-pick it."""
        return await anyio.to_thread.run_sync(
            _add_node_sync, scope, name, statement, rationale
        )

    def _add_citation_sync(node_id, cited_node_id, cited_decision_id):
        with session_scope() as session:
            return shadow.add_citation(
                session,
                node_id,
                cited_node_id=cited_node_id,
                cited_decision_id=cited_decision_id,
            )

    @mcp.tool()
    async def add_citation(
        node_id: str,
        cited_node_id: str | None = None,
        cited_decision_id: str | None = None,
    ) -> dict:
        """Record a citation FROM a shadow node: exactly one target — another node
        in the SAME branch (`cited_node_id`, the cherry-pick dependency) XOR a real
        decision (`cited_decision_id`, the permitted inward reference). The reverse
        never exists: nothing real may cite shadow."""
        return await anyio.to_thread.run_sync(
            _add_citation_sync, node_id, cited_node_id, cited_decision_id
        )

    def _list_citations_sync(node_id):
        with session_scope() as session:
            return {"citations": shadow.list_citations(session, node_id)}

    @mcp.tool()
    async def list_citations(node_id: str) -> dict:
        """The citations a node makes (its outgoing inward references) — the
        dependency set a future graduation's cherry-pick closure walks."""
        return await anyio.to_thread.run_sync(_list_citations_sync, node_id)

    def _corpus_search_sync(query, scope, limit):
        scope_id = None
        scope_slug = None
        if scope is not None:
            sc = _resolve_scope(scope)
            scope_id, scope_slug = sc["id"], sc["slug"]
        with session_scope() as session:
            return shadow.corpus_search(
                session, query, scope_id=scope_id, scope_slug=scope_slug,
                limit=limit,
            )

    @mcp.tool()
    async def shadow_corpus_search(
        query: str, scope: str | None = None, limit: int | None = None
    ) -> dict:
        """Shadow-scoped corpus search: full-text across the speculation surface's
        own content — shadow branches (active AND archived), their nodes,
        narrative-notes — bundled with the REAL decisions backlinked to a shadow
        line. Ranked headers merged across corpora; kinds: `shadow_branch`,
        `shadow_node`, `decision`. Pass `scope` (resolved against the platform) to
        narrow each corpus to that scope; omit it to search all shadow content.
        Raises on a blank query or unknown scope slug."""
        return await anyio.to_thread.run_sync(
            _corpus_search_sync, query, scope, limit
        )

    # Read-real grounding — the SAME decision + artifact READ handlers the `main`
    # surface registers (one source of truth). NO real-write verb is registered
    # on this surface; that absence IS the isolation guarantee (decision 8a7f0a11).
    _register_read_tools(mcp, client)

    return mcp
