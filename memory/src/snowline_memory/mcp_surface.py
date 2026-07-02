"""The memory MCP surface — the `main`-mapped working-memory tools.

Mirrors governance's FastMCP registration pattern (`mcp_surface.py`): a single
`main` surface (memory has no isolated `shadow` surface — every verb is a
first-class working-memory op). Each tool runs its blocking DB work in a thread
(`anyio.to_thread.run_sync`) so the async transport isn't blocked.

Docstrings are written for the AGENT reader — they instruct WHEN to reach for the
tool, not just what it does. `remember`'s in particular tells the agent to save
durable working context (conventions, gotchas, preferences), NOT things the
repo/git already record, and that writing the same name updates in place.
"""

from __future__ import annotations

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from snowline_memory import memory
from snowline_memory.db import session_scope

_INSTRUCTIONS = """\
This is the Snowline MEMORY surface — cross-folder, cross-machine agent SESSION \
MEMORY: the durable working context a session needs to be productive \
(conventions, gotchas, user preferences, current focus, references), reachable \
from any folder or machine. `memory_digest` is the SESSION-START read — call it \
at the top of a session for a one-line index of everything known. `recall` \
searches (full-text when you pass a query, newest-first otherwise). `remember` \
saves/updates a note (upsert by kebab `name`). `list_memories` / `forget` are \
hygiene. Memory is WORKING CONTEXT, distinct from governance DECISIONS: a memory \
that hardens into policy should GRADUATE to `record_decision` on the governance \
surface, not live in memory forever. Scopes are platform-owned; memory tags a \
note with an optional scope slug (a soft reference), portfolio-wide when omitted.\
"""

# DNS-rebinding protection off (governance's default) — behind the platform trust
# gate the plugin is reached on the tailnet.
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def build_main_surface() -> FastMCP:
    """Build the `main` FastMCP surface with the memory tools registered. Returns
    a fresh `FastMCP` so the app builds one and tests can build their own."""
    mcp = FastMCP(
        "snowline-memory",
        stateless_http=True,
        transport_security=_SECURITY,
        instructions=_INSTRUCTIONS,
    )

    def _remember_sync(content, name, description, kind, scope):
        with session_scope() as session:
            return memory.remember(
                session,
                content=content,
                name=name,
                description=description,
                kind=kind,
                scope=scope,
            )

    @mcp.tool()
    async def remember(
        content: str,
        name: str | None = None,
        description: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
    ) -> dict:
        """Save durable WORKING CONTEXT for future sessions — a convention, a
        gotcha, a user preference, the current focus, a useful reference. Do NOT
        save things the repo or git already record (file contents, commit
        history, what a function does) — save the non-obvious knowledge a fresh
        session would otherwise have to rediscover.

        UPSERTS by `name` (kebab-case): writing the SAME name UPDATES that memory
        in place — use this to refine a note rather than piling up duplicates.
        Omit `name` to auto-generate one from the description/content. `kind` is
        one of user/feedback/project/reference/gotcha (default `project`). `scope`
        is an optional Snowline slug (`org` or `org/repo`) tagging the note to a
        scope; omit it for portfolio-wide. If it has hardened into policy, prefer
        `record_decision` on the governance surface instead.
        """
        return await anyio.to_thread.run_sync(
            _remember_sync, content, name, description, kind, scope
        )

    def _recall_sync(query, kind, scope, limit):
        with session_scope() as session:
            return memory.recall(
                session, query=query, kind=kind, scope=scope, limit=limit
            )

    @mcp.tool()
    async def recall(
        query: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        limit: int = 10,
    ) -> dict:
        """Search working memory. Pass a `query` for full-text search (ranked by
        relevance over name + description + content); omit it to get the most
        recently-updated notes. Filter by `kind` and/or `scope` — a `scope`
        returns that scope's notes PLUS portfolio-wide ones. Returns the matching
        memories (full content) and `items_total`.
        """
        return await anyio.to_thread.run_sync(
            _recall_sync, query, kind, scope, limit
        )

    def _digest_sync(scope):
        with session_scope() as session:
            return memory.memory_digest(session, scope=scope)

    @mcp.tool()
    async def memory_digest(scope: str | None = None) -> dict:
        """The SESSION-START read — call this at the top of a session. Returns
        EVERY known memory as a cheap one-line `name — description` index, grouped
        by kind, so you can see what's known at a glance and `recall` the full
        body of anything relevant. With a `scope` it narrows to that scope's notes
        plus portfolio-wide ones; without, it returns everything. Deterministic
        and cheap — safe to call every session (it's the compensation for the
        harness not auto-injecting memory).
        """
        return await anyio.to_thread.run_sync(_digest_sync, scope)

    def _list_sync(kind, scope, limit):
        with session_scope() as session:
            return memory.list_memories(
                session, kind=kind, scope=scope, limit=limit
            )

    @mcp.tool()
    async def list_memories(
        kind: str | None = None,
        scope: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Browse memory HEADERS (name, description, kind, scope, updated_at),
        newest-first — the hygiene/browse view. Filter by `kind` and/or `scope`
        (scope includes portfolio-wide). Use `recall` to read a note's full body,
        `forget` to remove one.
        """
        return await anyio.to_thread.run_sync(_list_sync, kind, scope, limit)

    def _forget_sync(name):
        with session_scope() as session:
            return memory.forget(session, name)

    @mcp.tool()
    async def forget(name: str) -> dict:
        """Delete one memory by `name`. Use for stale or wrong notes. Idempotent —
        forgetting a name that doesn't exist reports `forgotten: false` rather
        than erroring.
        """
        return await anyio.to_thread.run_sync(_forget_sync, name)

    return mcp
