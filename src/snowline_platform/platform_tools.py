"""The platform's OWN MCP tool surface — scope + milestone registry verbs served
by the platform REGISTERING ITSELF AS AN UPSTREAM (governance decision 0503fff0).

The platform has native identity primitives (scopes, milestones) but every tool
Snowline exposes reaches the agent through the gateway's ordinary aggregation of
registered plugin surfaces. So rather than special-case platform tools inside the
aggregator — which would break its invariant of "composes whole surfaces, never
reasons about individual tools" (gateway.md §2) and stand up a second
tool-serving mechanism — the platform serves these tools the SAME way a plugin
does: a streamable-HTTP MCP app mounted at `/platform/mcp`, plus a `platform`
registry entry at the platform's own loopback base_url mapping
`{"/platform/mcp": "main"}`. The gateway then composes it onto `main` through the
same `discover_upstreams` path as any plugin, and the tools surface namespaced
`platform__<tool>` by the existing `<plugin>__<tool>` convention. This has exact
precedent: the platform already self-participates in replication as "the SDK's
own publisher" (replication-continuity §8 self-manifest).

The tools are THIN wrappers over `scopes` / `milestones` — the platform CAN
import its own services (unlike an out-of-process plugin, which reaches them over
HTTP), so each tool calls the service directly under a `session_scope()`,
running the blocking DB work in a thread (`anyio.to_thread.run_sync`, the
governance surface's pattern) so the async transport isn't blocked. NO new
business logic lives here.

**Errors → MCP tool errors, message text preserved.** A tool lets its service
exception propagate; FastMCP turns it into an `isError` `CallToolResult` whose
text is the exception's message (`str(exc)`), which the gateway returns verbatim.
The service messages are already agent-facing contract text — in particular a
`MilestoneResolutionError` bakes its near-miss SUGGESTIONS into the message via
`milestones._suggestion_tail` ("… did you mean <addr> (<status>)?"), so the
suggestions survive into the error payload without any structured-error plumbing
(the same posture the HTTP resolve route takes, just text-only rather than a
`{detail, suggestions}` body). This mirrors governance's surfaces exactly, where
a `ScopeNotFoundError` surfaces the same wrapped way.
"""

from __future__ import annotations

from datetime import date

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from snowline_platform import config, milestones, scopes
from snowline_platform.db import session_scope
from snowline_platform.gateway import ROOT_SURFACE
from snowline_platform.manifest import PluginManifest

# The registry name of the platform's self-entry — the namespace prefix its tools
# surface under (`platform__<tool>`) and the routing key the gateway resolves.
# A url-safe slug (manifest.PLUGIN_NAME_RE), so `__` never occurs inside it and
# the gateway's first-`__` split is unambiguous.
PLATFORM_PLUGIN_NAME = "platform"

# Where the platform mounts its own tool app on its HTTP surface, and the single
# key of the self-entry's surface map — `{PLATFORM_MCP_PATH: ROOT_SURFACE}`, so
# the tools compose onto `main` and nowhere else (an isolation surface like
# `shadow` gets NO platform tools, by composition alone).
PLATFORM_MCP_PATH = "/platform/mcp"

_INSTRUCTIONS = """\
This is the Snowline PLATFORM surface — the platform's OWN identity primitives, \
scopes and milestones, served natively (not by any plugin). SCOPES are the \
universal addressing tree (`owner/repo`, initiatives, components): `list_scopes`, \
`resolve_scope` (non-mutating lookup — never auto-creates), `scope_tree`, \
`scope_ancestors` (the isolation-halting applicability chain a reader inherits \
UPWARD), `create_scope` / `update_scope`. MILESTONES are the portfolio's \
cross-plugin release-correlation keys, addressed `<anchor>/<name>`: \
`create_milestone` (the only mint path — born `planned`), `resolve_milestone` \
(shorthand is legitimate input, storage is canonical, unknown hard-fails with \
suggestions and NEVER mints), `list_milestones`, the lifecycle verbs \
`activate_milestone`/`deactivate_milestone`/`achieve_milestone`/\
`cancel_milestone` (explicit, never automatic; activate a release when work on it \
STARTS — an active milestone is its scope's current release), `get_milestone` (audit read — returns a merge tombstone as itself), \
`update_milestone` (outcome/target_date only, `""` clears), and \
`milestone_transitions` (the append-only lifecycle log). Drift reconciliation: \
`merge_milestone` aliases one milestone into another (state-compatible only; the \
tombstone reserves its name forever), `milestone_aliases` reads a target's \
tombstone closure, and `add_milestone_dependency`/`remove_milestone_dependency`/\
`milestone_dependencies` manage the cycle-guarded, readiness-only dependency DAG \
(cross-anchor edges allowed). Release line (ship order per anchor, independent \
of dependencies): `place_in_line` / `remove_from_line`, read with \
`list_milestones(anchor=, in_line=True)`. Replication conflicts (lifecycle \
contradictions between partitions; empty in steady state): \
`list_milestone_conflicts` / `resolve_milestone_conflict`. Slugs and names are case-insensitive on input and \
stored canonical-lowercase.\
"""

# DNS-rebinding protection off on the streamable-HTTP transport, matching the
# gateway's own surfaces (gateway_app._SECURITY): the platform sits behind the
# platform trust gate (tailnet + loopback, decision 35546152), and the gateway
# dials this app over loopback.
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _parse_date(value: str | None) -> date | None:
    """An ISO `target_date` string → `date`, mirroring the milestones HTTP route's
    `date.fromisoformat` parse. Surfaces a clean message on a bad value (the
    exception text becomes the tool error)."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid target_date {value!r} — expected ISO YYYY-MM-DD") from exc


def build_platform_tools_surface() -> FastMCP:
    """The platform's native tool surface as a `FastMCP` — every scope + milestone
    verb registered as a thin wrapper over the service (scope-namespace.md §4,
    milestones.md §5). Returns a fresh instance so the app mounts one and tests
    can build their own. Mounted via its low-level server (`._mcp_server`) on a
    `StreamableHTTPSessionManager`, exactly like an aggregated gateway surface."""
    mcp = FastMCP(
        "snowline-platform",
        stateless_http=True,
        transport_security=_SECURITY,
        instructions=_INSTRUCTIONS,
    )

    # --- scopes (scope-namespace.md §4) -------------------------------------

    def _list_scopes_sync(org: str | None) -> dict:
        with session_scope() as session:
            return {"scopes": scopes.list_scopes(session, org=org)}

    @mcp.tool()
    async def list_scopes(org: str | None = None) -> dict:
        """List every registered scope as a lightweight row (slug, name, kind,
        derived org, status, isolated), slug-ordered. `org` narrows to one org
        (the first slug segment). Read-only."""
        return await anyio.to_thread.run_sync(_list_scopes_sync, org)

    def _resolve_scope_sync(slug: str) -> dict:
        with session_scope() as session:
            scope = scopes.resolve(session, slug)
            if scope is None:
                raise scopes.ScopeNotFoundError(
                    f"no scope with slug {slug!r} — register it on the platform "
                    "first (nothing auto-vivifies)"
                )
            return scopes.to_row(scope)

    @mcp.tool()
    async def resolve_scope(slug: str) -> dict:
        """Resolve one scope slug to its full row (id, slug, name, kind, status,
        isolated, org). Case-insensitive input, canonical-lowercase storage.
        NON-MUTATING — an unknown slug hard-fails (no implicit stub creation;
        create is explicit via `create_scope`). Read-only."""
        return await anyio.to_thread.run_sync(_resolve_scope_sync, slug)

    def _scope_tree_sync(root: str | None) -> dict:
        with session_scope() as session:
            return {"tree": scopes.tree(session, root=root)}

    @mcp.tool()
    async def scope_tree(root: str | None = None) -> dict:
        """The scope forest as nested `parent_id`-edged trees — each node
        `{slug, name, kind, status, isolated, children}`, slug-ordered. `root` (a
        slug) returns just that scope's subtree; omit it for the whole forest.
        `isolated` marks the inheritance boundary a reader reasons about. Raises on
        an unknown `root`. Read-only."""
        return await anyio.to_thread.run_sync(_scope_tree_sync, root)

    def _scope_ancestors_sync(slug: str) -> dict:
        with session_scope() as session:
            scope = scopes.resolve(session, slug)
            if scope is None:
                raise scopes.ScopeNotFoundError(f"no scope with slug {slug!r}")
            return {
                "ancestors": [scopes.to_row(s) for s in scopes.ancestors(session, scope)]
            }

    @mcp.tool()
    async def scope_ancestors(slug: str) -> dict:
        """The APPLICABILITY chain for a scope: the scope itself then each
        `parent_id` ancestor, nearest-first, HALTING at the first `isolated` node
        and the forest root — the UPWARD governance a reader at this slug inherits
        (an isolated scope blocks inheritance from above it). Raises on an unknown
        slug. Read-only."""
        return await anyio.to_thread.run_sync(_scope_ancestors_sync, slug)

    def _create_scope_sync(
        slug: str, name: str, kind: str, parent: str | None, isolated: bool
    ) -> dict:
        with session_scope() as session:
            # `parent=None` means "not provided" here (mirroring the HTTP create
            # route): omit the kwarg so `create` derives the parent from the
            # slug's prefix, rather than its explicit-None "no parent, no
            # derivation" meaning (the replication apply seam's distinct case).
            kwargs = {"parent": parent} if parent is not None else {}
            scope = scopes.create(
                session, slug=slug, name=name, kind=kind, isolated=isolated, **kwargs
            )
            return scopes.to_row(scope)

    @mcp.tool()
    async def create_scope(
        slug: str,
        name: str,
        kind: str,
        parent: str | None = None,
        isolated: bool = False,
    ) -> dict:
        """Create a scope. `kind` ∈ project/component/topic/initiative/org, with
        the bare-slug⇔org invariant (a bare org slug, no `/`, must be kind `org`;
        `org` is valid only for a bare slug). `parent` omitted DERIVES the parent
        from the slug's hierarchical prefix (linking to that row if it exists);
        pass a slug to link an explicit existing parent. `isolated` blocks
        governance inheritance from above. Fails on a bad slug/kind or a slug
        already taken. Returns the created row."""
        return await anyio.to_thread.run_sync(
            _create_scope_sync, slug, name, kind, parent, isolated
        )

    def _update_scope_sync(
        slug: str,
        name: str | None,
        kind: str | None,
        parent: str | None,
        isolated: bool | None,
        status: str | None,
    ) -> dict:
        with session_scope() as session:
            # A None arg means "leave unchanged" (the service's own default for
            # name/kind/isolated/status). For `parent`, None also means unchanged
            # (omit the kwarg so it stays the service's `_UNSET`); pass "" to CLEAR
            # (detach) or a slug to re-point — MCP can't send a Python sentinel, so
            # "" is the documented clear signal, matching the service's `in (None,
            # "")` clear rule.
            kwargs: dict = {}
            if name is not None:
                kwargs["name"] = name
            if kind is not None:
                kwargs["kind"] = kind
            if isolated is not None:
                kwargs["isolated"] = isolated
            if status is not None:
                kwargs["status"] = status
            if parent is not None:
                kwargs["parent"] = parent
            scope = scopes.update(session, slug, **kwargs)
            return scopes.to_row(scope)

    @mcp.tool()
    async def update_scope(
        slug: str,
        name: str | None = None,
        kind: str | None = None,
        parent: str | None = None,
        isolated: bool | None = None,
        status: str | None = None,
    ) -> dict:
        """Modify an existing scope. Only arguments you pass change; an omitted
        (null) argument is left as-is. `kind` still enforces the bare-slug⇔org
        invariant. `parent`: pass a slug to re-point to that existing scope, `""`
        to clear (detach), or omit to leave unchanged. Raises on an unknown slug or
        an unknown parent. Returns the refreshed row."""
        return await anyio.to_thread.run_sync(
            _update_scope_sync, slug, name, kind, parent, isolated, status
        )

    # --- milestones (milestones.md §5) --------------------------------------

    def _create_milestone_sync(
        anchor: str, name: str, outcome: str | None, target_date: str | None
    ) -> dict:
        parsed = _parse_date(target_date)
        with session_scope() as session:
            m = milestones.create(
                session, anchor=anchor, name=name, outcome=outcome, target_date=parsed
            )
            return milestones.to_row(m)

    @mcp.tool()
    async def create_milestone(
        anchor: str,
        name: str,
        outcome: str | None = None,
        target_date: str | None = None,
    ) -> dict:
        """Mint a milestone — the ONLY create path. `anchor` is a REGISTERED
        1-or-2-segment scope (org- or repo-level; no portfolio/global anchor);
        `name` is a slash-free lowercase slug (the address is `<anchor>/<name>`).
        Every milestone is born `planned` — lifecycle is explicit verbs, never
        automatic. `outcome` is the human "done means" line; `target_date` an
        optional ISO YYYY-MM-DD. Fails on an unregistered anchor, a bad name, or a
        duplicate (a merge tombstone reserves the name forever). Returns the row.

        Milestones are RELEASES ONLY (e.g. `v1.5`) — never capability, feature or
        gate milestones; capabilities/outcomes belong in PM initiatives and phases
        (governance decision 0fda34e5)."""
        return await anyio.to_thread.run_sync(
            _create_milestone_sync, anchor, name, outcome, target_date
        )

    def _resolve_milestone_sync(ref: str, context: str | None) -> dict:
        with session_scope() as session:
            m, via_alias = milestones.resolve_row(session, ref, context)
            row = milestones.to_row(m)
            row["resolved_via_alias"] = via_alias
            return row

    @mcp.tool()
    async def resolve_milestone(ref: str, context: str | None = None) -> dict:
        """Resolve a milestone reference to its canonical row, following any merge
        alias (`resolved_via_alias` flags a tombstone hop). Shorthand is a
        legitimate INPUT format but STORAGE is canonical: a 2-/3-segment address
        (`<org>/<name>` or `<org>/<repo>/<name>`) resolves directly; a BARE name
        REQUIRES `context` (a scope slug) and walks it repo-then-org,
        most-specific-first. A bare name with no context, and any unknown ref,
        HARD-FAIL with near-miss suggestions in the error — nothing is EVER
        minted. Returns the full row plus `resolved_via_alias`."""
        return await anyio.to_thread.run_sync(_resolve_milestone_sync, ref, context)

    def _list_milestones_sync(
        anchor: str | None, status: str | None, include_merged: bool, in_line: bool
    ) -> dict:
        with session_scope() as session:
            return {
                "milestones": milestones.list_milestones(
                    session,
                    anchor=anchor,
                    status=status,
                    include_merged=include_merged,
                    in_line=in_line,
                )
            }

    @mcp.tool()
    async def list_milestones(
        anchor: str | None = None,
        status: str | None = None,
        include_merged: bool = False,
        in_line: bool = False,
    ) -> dict:
        """List registry rows, address-ordered (distinct from PM's work roll-up
        read of the same name — the `platform__` prefix disambiguates). `anchor`
        SUBTREE-filters (the given scope and everything below it, so an org anchor
        surfaces its repo-anchored milestones too); `status` filters by lifecycle
        status. Merge tombstones are excluded unless `include_merged=True`.
        `in_line=True` (requires `anchor`) instead returns that anchor's RELEASE
        LINE: only ranked rows anchored EXACTLY there (no subtree), in line order
        — the line says which release ships next, while `depends_on` says which
        release needs another's work. Read-only. Milestones are releases only
        (decision 0fda34e5)."""
        return await anyio.to_thread.run_sync(
            _list_milestones_sync, anchor, status, include_merged, in_line
        )

    def _place_in_line_sync(
        address: str, after: str | None, before: str | None
    ) -> dict:
        with session_scope() as session:
            return milestones.place_in_line(
                session, address, after=after, before=before
            )

    @mcp.tool()
    async def place_in_line(
        address: str, after: str | None = None, before: str | None = None
    ) -> dict:
        """Place a milestone in its anchor's RELEASE LINE — the ordered sequence
        of releases that says which one ships next. That is different from
        `depends_on`, which says one release needs another's work and never
        orders the line. Pass at most one of `after` / `before` (an address, or a
        bare name resolved against this milestone's anchor); with neither the
        milestone goes to the END of the line. Re-placing a member moves it. The
        neighbour must share the anchor and already be in the line (the error
        lists the line); a merge tombstone cannot be placed. Any status may be
        ranked and placing never activates or achieves anything. Returns the row
        plus `line` (addresses in order)."""
        return await anyio.to_thread.run_sync(
            _place_in_line_sync, address, after, before
        )

    def _remove_from_line_sync(address: str) -> dict:
        with session_scope() as session:
            return milestones.remove_from_line(session, address)

    @mcp.tool()
    async def remove_from_line(address: str) -> dict:
        """Take a milestone out of its anchor's RELEASE LINE (the ship-order
        sequence; unrelated to `depends_on` edges, which are left untouched).
        Idempotent on a milestone that is not in the line; rejected on a merge
        tombstone. Returns the row plus `line` (addresses in order)."""
        return await anyio.to_thread.run_sync(_remove_from_line_sync, address)

    def _get_milestone_sync(address: str) -> dict:
        with session_scope() as session:
            return milestones.to_row(milestones.get(session, address))

    @mcp.tool()
    async def get_milestone(address: str) -> dict:
        """Read the milestone at its DIRECT 2-/3-segment `address` — the audit
        read: it does NOT follow a merge alias (that is `resolve_milestone`'s job),
        so a tombstone is returned AS ITSELF (its `merged_into` names the alias
        target). Raises if unknown. Read-only."""
        return await anyio.to_thread.run_sync(_get_milestone_sync, address)

    def _milestone_transitions_sync(address: str) -> dict:
        with session_scope() as session:
            return {"transitions": milestones.transitions(session, address)}

    @mcp.tool()
    async def milestone_transitions(address: str) -> dict:
        """The append-only lifecycle transition log for a milestone, oldest-first —
        each entry `{from_status, to_status, reason, authored_at}`. Raises if the
        address is unknown. Read-only."""
        return await anyio.to_thread.run_sync(_milestone_transitions_sync, address)

    def _list_conflicts_sync(anchor: str | None, include_resolved: bool) -> dict:
        with session_scope() as session:
            rows = milestones.list_unreconciled(
                session, anchor=anchor, include_resolved=include_resolved
            )
            return {"conflicts": rows, "count": len(rows)}

    @mcp.tool()
    async def list_milestone_conflicts(
        anchor: str | None = None, include_resolved: bool = False
    ) -> dict:
        """List milestone replication CONFLICTS awaiting triage (§9). A conflict is
        a lifecycle contradiction between partitions: two instances transitioned
        the same milestone concurrently, the row converged by last-writer-wins,
        but the log's last transition disagrees with the applied row (the
        converged history implies a move illegal under §4, e.g. cancelled→active).
        Each row has `id`, `milestone`, `detail` (the earlier/later transitions)
        and, once closed, `disposition`. Expected EMPTY in steady state. Resolve
        each with `resolve_milestone_conflict`. `anchor` subtree-filters;
        `include_resolved` also lists closed ones. Read-only."""
        return await anyio.to_thread.run_sync(
            _list_conflicts_sync, anchor, include_resolved
        )

    def _resolve_conflict_sync(
        id: str, disposition: str, reason: str, actor: str | None
    ) -> dict:
        with session_scope() as session:
            return milestones.resolve_unreconciled(
                session, id, disposition, reason, actor=actor
            )

    @mcp.tool()
    async def resolve_milestone_conflict(
        id: str, disposition: str, reason: str, actor: str | None = None
    ) -> dict:
        """Close a milestone conflict (see `list_milestone_conflicts`) with a
        disposition and a REQUIRED `reason`. `keep_row`: accept the applied row as
        truth. `replay_transition`: re-apply the logged later transition as a NEW
        transition authored now (replicates with a fresh lifecycle stamp); rejected
        if the row already has that status or the move is illegal from it.
        `dismiss`: close with no row change. Closed flags stay as local triage
        history. Returns the closed conflict row."""
        return await anyio.to_thread.run_sync(
            _resolve_conflict_sync, id, disposition, reason, actor
        )

    def _lifecycle_sync(verb, address: str, reason: str | None) -> dict:
        with session_scope() as session:
            m = verb(session, address, reason=reason)
            row = milestones.to_row(m)
            # §4 warnings (unmet deps on activate/achieve; cancel-from-active
            # governance demotion) — attached only when non-empty.
            warnings = milestones.transition_warnings(session, m)
            if warnings:
                row["warnings"] = warnings
            return row

    @mcp.tool()
    async def activate_milestone(address: str, reason: str | None = None) -> dict:
        """Move a milestone planned→active (§4): this release is now CURRENT for
        its scope. Activate when work on it STARTS, not at ship time (decision
        68f4ed4b) — PM's whats_next/briefing focus on active releases, and several
        may be active per anchor (parallel release lines). Side effect: governance
        spec versions stamped with this milestone become canonical (§6.1).
        `deactivate_milestone` undoes a mistaken activation. Rejects any source
        status but `planned` — never auto-activates. `reason` is recorded on the
        transition. Returns the updated row."""
        return await anyio.to_thread.run_sync(
            _lifecycle_sync, milestones.activate, address, reason
        )

    @mcp.tool()
    async def deactivate_milestone(address: str, reason: str) -> dict:
        """Undo a mistaken activation: move a milestone active→planned (§4,
        decision 68f4ed4b). Only an `active` milestone deactivates; anything else
        is rejected. `reason` is REQUIRED and recorded on the transition.
        `activated_at` is cleared, the release stops being current in PM, and
        governance versions stamped with it revert to pending (§6.1). Returns the
        updated row."""
        return await anyio.to_thread.run_sync(
            _lifecycle_sync, milestones.deactivate, address, reason
        )

    @mcp.tool()
    async def achieve_milestone(address: str, reason: str | None = None) -> dict:
        """Move a milestone active→achieved (§4) — the SHIP step for a release
        activated when work on it began. Achieving a still-`planned` milestone is
        REJECTED — activate first; achievement is never automatic and no
        member-item state ever implies it. `reason` is recorded. Returns the
        updated row."""
        return await anyio.to_thread.run_sync(
            _lifecycle_sync, milestones.achieve, address, reason
        )

    @mcp.tool()
    async def cancel_milestone(address: str, reason: str | None = None) -> dict:
        """Cancel a milestone planned|active→cancelled (§4) — a deliberate
        retraction. Rejected from a terminal status. `reason` is recorded. Returns
        the updated row."""
        return await anyio.to_thread.run_sync(
            _lifecycle_sync, milestones.cancel, address, reason
        )

    def _update_milestone_sync(
        address: str, outcome: str | None, target_date: str | None
    ) -> dict:
        # Body-present-key semantics MCP can't express natively (no way to send a
        # Python sentinel), so mirror `update_scope`: None = leave unchanged; the
        # empty string "" = the documented CLEAR signal (→ service None, which
        # clears). A non-empty `target_date` parses as ISO.
        kwargs: dict = {}
        if outcome is not None:
            kwargs["outcome"] = None if outcome == "" else outcome
        if target_date is not None:
            kwargs["target_date"] = None if target_date == "" else _parse_date(
                target_date
            )
        with session_scope() as session:
            return milestones.to_row(milestones.update(session, address, **kwargs))

    @mcp.tool()
    async def update_milestone(
        address: str,
        outcome: str | None = None,
        target_date: str | None = None,
    ) -> dict:
        """Modify a milestone's display fields — `outcome` / `target_date` — NEVER
        its identity (§4). Only arguments you pass change: an omitted (null)
        argument is left as-is, and the empty string `""` CLEARS that field (for
        `target_date`, `""` clears; a non-empty value is ISO YYYY-MM-DD). Raises if
        the address is unknown. Returns the refreshed row. Milestones are releases
        only; keep `outcome` a release outcome, not a capability (decision 0fda34e5)."""
        return await anyio.to_thread.run_sync(
            _update_milestone_sync, address, outcome, target_date
        )

    def _merge_milestone_sync(from_address: str, into_address: str) -> dict:
        with session_scope() as session:
            return milestones.merge(session, from_address, into_address)

    @mcp.tool()
    async def merge_milestone(from_address: str, into_address: str) -> dict:
        """Merge one milestone into another (§7): mark `from_address` an ALIAS
        TOMBSTONE resolving to `into_address` forever. `into` is resolved to its
        terminal target first (chains stay depth-1); the tombstone stays at
        `from`'s anchor, reserving that name. Legal only when the two statuses
        MATCH or `from` is still `planned` (else the merge would rewrite governance
        history — rejected). `from`'s dependency edges re-point to `into` and the
        cycle guard re-runs (merge fails whole if the union would cycle). Returns
        `{tombstone, target, reminder}` — the reminder flags that the platform
        never bulk-retags plugin data, so review affected consumer rows agent-side.
        """
        return await anyio.to_thread.run_sync(
            _merge_milestone_sync, from_address, into_address
        )

    def _add_dependency_sync(dependent: str, dependency: str) -> dict:
        with session_scope() as session:
            return milestones.add_dependency(session, dependent, dependency)

    @mcp.tool()
    async def add_milestone_dependency(dependent: str, dependency: str) -> dict:
        """Add a dependency edge — `dependent` depends on `dependency` (§2/§4).
        Both refs resolve through any merge alias. Cross-anchor edges are allowed
        (the anchor is not a fence); the edge is cycle-guarded over the GLOBAL
        dependency DAG and rejected if it would cycle. A self-edge is rejected; a
        duplicate is idempotent (no-op success). Dependencies gate READINESS reads
        only — they never block lifecycle verbs. Returns `dependent`'s current
        `{depends_on, dependents}`."""
        return await anyio.to_thread.run_sync(
            _add_dependency_sync, dependent, dependency
        )

    def _remove_dependency_sync(dependent: str, dependency: str) -> dict:
        with session_scope() as session:
            return milestones.remove_dependency(session, dependent, dependency)

    @mcp.tool()
    async def remove_milestone_dependency(dependent: str, dependency: str) -> dict:
        """Remove the `dependent → dependency` edge (§4). Both refs resolve through
        any merge alias. Idempotent — removing an absent edge is a no-op success.
        Returns `dependent`'s current `{depends_on, dependents}`."""
        return await anyio.to_thread.run_sync(
            _remove_dependency_sync, dependent, dependency
        )

    def _milestone_dependencies_sync(address: str) -> dict:
        with session_scope() as session:
            return milestones.dependencies(session, address)

    @mcp.tool()
    async def milestone_dependencies(address: str) -> dict:
        """Both directions of a milestone's dependency edges (§4): `depends_on`
        (what it depends on) and `dependents` (what depends on it), each
        `{address, status}`. The raw status is always surfaced — a dependency on a
        CANCELLED milestone stays visible (PM flags it `blocked_by_cancelled`). The
        address resolves through any merge alias. Read-only."""
        return await anyio.to_thread.run_sync(
            _milestone_dependencies_sync, address
        )

    def _milestone_aliases_sync(address: str) -> dict:
        with session_scope() as session:
            return milestones.aliases(session, address)

    @mcp.tool()
    async def milestone_aliases(address: str) -> dict:
        """The tombstone closure resolving to `address`'s terminal target (§5):
        `{target, aliases}`, where `aliases` is every merged-away address pointing
        at that milestone. Consumer reads match stored stamps/tags against this
        full alias set so reads via either address agree. Read-only."""
        return await anyio.to_thread.run_sync(_milestone_aliases_sync, address)

    return mcp


def platform_self_manifest() -> PluginManifest:
    """The `platform` registry entry that makes the platform its OWN upstream
    (decision 0503fff0): its loopback `base_url` (config.platform_self_url), the
    `/platform/mcp` tool app mapped onto `main` (`ROOT_SURFACE`), and NOTHING
    onto any isolation surface — so the native tools compose onto `main` and only
    `main`, through the ordinary gateway path.

    It is an ORDINARY manifest: the gateway's `discover_upstreams` and the health
    poller treat it like any plugin (no special-casing anywhere). `mcp_path` /
    `health_path` default to `/mcp` / `/health` — `health_path` deliberately kept
    the default so the poller checks the platform's own `/health` on loopback
    (which answers 200 while the platform is serving); it is NOT the tool path."""
    return PluginManifest(
        name=PLATFORM_PLUGIN_NAME,
        base_url=config.platform_self_url(),
        surfaces={PLATFORM_MCP_PATH: ROOT_SURFACE},
    )
