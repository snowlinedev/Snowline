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
`activate_milestone`/`achieve_milestone`/`cancel_milestone` (explicit, never \
automatic), `get_milestone` (audit read — returns a merge tombstone as itself), \
and `milestone_transitions` (the append-only lifecycle log). Slugs and names are \
case-insensitive on input and stored canonical-lowercase.\
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
        duplicate (a merge tombstone reserves the name forever). Returns the row."""
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
        anchor: str | None, status: str | None, include_merged: bool
    ) -> dict:
        with session_scope() as session:
            return {
                "milestones": milestones.list_milestones(
                    session, anchor=anchor, status=status, include_merged=include_merged
                )
            }

    @mcp.tool()
    async def list_milestones(
        anchor: str | None = None,
        status: str | None = None,
        include_merged: bool = False,
    ) -> dict:
        """List registry rows, address-ordered (distinct from PM's work roll-up
        read of the same name — the `platform__` prefix disambiguates). `anchor`
        SUBTREE-filters (the given scope and everything below it, so an org anchor
        surfaces its repo-anchored milestones too); `status` filters by lifecycle
        status. Merge tombstones are excluded unless `include_merged=True`.
        Read-only."""
        return await anyio.to_thread.run_sync(
            _list_milestones_sync, anchor, status, include_merged
        )

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

    def _lifecycle_sync(verb, address: str, reason: str | None) -> dict:
        with session_scope() as session:
            return milestones.to_row(verb(session, address, reason=reason))

    @mcp.tool()
    async def activate_milestone(address: str, reason: str | None = None) -> dict:
        """Move a milestone planned→active (§4). Rejects any other source status —
        never auto-activates. `reason` is recorded on the transition. Returns the
        updated row."""
        return await anyio.to_thread.run_sync(
            _lifecycle_sync, milestones.activate, address, reason
        )

    @mcp.tool()
    async def achieve_milestone(address: str, reason: str | None = None) -> dict:
        """Move a milestone active→achieved (§4). Achieving a still-`planned`
        milestone is REJECTED — activate first; achievement is never automatic and
        no member-item state ever implies it. `reason` is recorded. Returns the
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
