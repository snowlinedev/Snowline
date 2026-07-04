"""The `/ui-api` routes (ui_api.py, ui-shell.md §4.1/§4.2, issue #55) — the
FIRST registered UI contribution. Asserts contract fidelity against seeded
shadow data: the exact keys the dashboard's kind validators require
(dashboard/src/kinds/kinds.tsx `validateStat`/`validateTableData`/
`validateThreadData`), plus the empty-DB and unknown-branch-id edge cases.

Writes are seeded through a DIRECT `session_scope()` call (not the `db_session`
fixture, which holds one uncommitted session for the whole test) so the data is
committed before the HTTP request opens its own session — production's shape,
where the route and the seeding are different connections.
"""

from __future__ import annotations

import uuid

import anyio
import httpx

from snowline_governance import shadow
from snowline_governance.app import create_app
from snowline_governance.db import session_scope


def _sid(slug: str) -> uuid.UUID:
    """The stable per-slug scope id (matches `StubScopeClient` elsewhere)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


class _NoopScopeClient:
    def resolve(self, slug: str): return None
    def ancestors(self, slug: str): return []


def _app(monkeypatch):
    monkeypatch.setenv("SNOWLINE_WEBHOOK_DISABLED", "1")
    return create_app(
        scope_client=_NoopScopeClient(),
        migrate_on_startup=False,
        register_on_startup=False,
    )


def _http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gov",
        timeout=httpx.Timeout(30.0),
    )


async def _get(app, path: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with _http(app) as http:
            return await http.get(path)


def _get_sync(app, path: str) -> httpx.Response:
    return anyio.run(_get, app, path)


async def _post(app, path: str, json_body: dict) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with _http(app) as http:
            return await http.post(path, json=json_body)


def _post_sync(app, path: str, json_body: dict) -> httpx.Response:
    return anyio.run(_post, app, path, json_body)


# --- widget: stat --------------------------------------------------------


def test_shadow_activity_widget_counts_open_branches_across_scopes(
    monkeypatch, clean_db
):
    with session_scope() as s:
        shadow.create_branch(s, "acme/widget", _sid("acme/widget"), "line-a")
        shadow.create_branch(s, "acme/widget", _sid("acme/widget"), "line-b")
        shadow.create_branch(s, "acme/other", _sid("acme/other"), "line-c")
    with session_scope() as s:
        # archived branches are NOT "open" — the stat excludes them.
        shadow.archive_branch(s, "acme/other", "line-c")

    resp = _get_sync(_app(monkeypatch), "/ui-api/widgets/shadow-activity")
    assert resp.status_code == 200
    assert resp.json() == {"value": 2, "label": "open shadow branches"}


def test_shadow_activity_widget_empty_db(monkeypatch, clean_db):
    resp = _get_sync(_app(monkeypatch), "/ui-api/widgets/shadow-activity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["value"] == 0
    assert body["label"] == "open shadow branches"


# --- page: branches table -------------------------------------------------


def test_branches_table_contract_fidelity(monkeypatch, clean_db):
    with session_scope() as s:
        shadow.create_branch(
            s, "acme/widget", _sid("acme/widget"), "line-a", "some notes"
        )
        shadow.add_node(s, "acme/widget", "line-a", "statement one")

    resp = _get_sync(_app(monkeypatch), "/ui-api/pages/branches")
    assert resp.status_code == 200
    body = resp.json()

    # Exactly the keys the shell's table validator requires.
    assert set(body.keys()) >= {"columns", "rows"}
    for column in body["columns"]:
        assert "key" in column and "label" in column

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert set(row.keys()) >= {"cells"}
    cells = row["cells"]
    assert cells["branch"] == "line-a"
    assert cells["scope"] == "acme/widget"
    assert cells["status"] == "active"
    assert cells["nodes"] == 1
    assert cells["updated"]  # an ISO timestamp string

    # The row href is plugin-relative and keyed on the branch's stable id —
    # round-trips through the thread route.
    assert row["href"].startswith("/shadow/")
    branch_id = row["href"].removeprefix("/shadow/")
    uuid.UUID(branch_id)  # raises if not a valid uuid


def test_branches_table_empty_db(monkeypatch, clean_db):
    resp = _get_sync(_app(monkeypatch), "/ui-api/pages/branches")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert body["empty"]
    assert len(body["columns"]) > 0


# --- page: one branch's thread --------------------------------------------


def test_branch_thread_contract_fidelity_with_narrative_and_citations(
    monkeypatch, clean_db
):
    # Each node is added in its OWN commit — `_branch_nodes` orders by
    # `(created_at, id)`, and two nodes minted in the SAME transaction share
    # one `func.now()` timestamp, leaving the tiebreak (a random uuid4) to
    # decide order; separate commits give them distinct timestamps so the
    # creation order this test asserts is deterministic.
    with session_scope() as s:
        branch = shadow.create_branch(
            s,
            "acme/widget",
            _sid("acme/widget"),
            "line-a",
            "the reasoning so far",
        )
    with session_scope() as s:
        n1 = shadow.add_node(s, "acme/widget", "line-a", "decision one", "why one")
    with session_scope() as s:
        n2 = shadow.add_node(s, "acme/widget", "line-a", "decision two", "why two")
    with session_scope() as s:
        shadow.add_citation(s, n2["id"], cited_node_id=n1["id"])

    resp = _get_sync(
        _app(monkeypatch), f"/ui-api/pages/branches/{branch['id']}"
    )
    assert resp.status_code == 200
    body = resp.json()

    # Exactly the keys the shell's thread validator requires.
    assert body["title"] == "line-a"
    assert "meta" in body
    assert "acme/widget" in body["meta"]
    assert "active" in body["meta"]
    assert "has narrative notes" in body["meta"]

    nodes = body["nodes"]
    assert len(nodes) == 3
    for node in nodes:
        assert set(node.keys()) >= {"author", "kind", "markdown", "at"}

    # The narrative notes come FIRST, as a synthetic node — one page tells the
    # whole story with no shell change.
    first = nodes[0]
    assert first["author"] == "narrative"
    assert first["kind"] == "notes"
    assert first["markdown"] == "the reasoning so far"

    # Then the shadow nodes, in creation order, statement+rationale as markdown.
    second, third = nodes[1], nodes[2]
    assert second["author"] == "shadow"
    assert second["kind"] == "node"
    assert "decision one" in second["markdown"]
    assert "why one" in second["markdown"]
    assert "citations" not in second  # n1 makes no citation

    assert "decision two" in third["markdown"]
    assert third["citations"] == [f"node:{n1['id']}"]


def test_branch_thread_no_narrative_notes_no_synthetic_node(monkeypatch, clean_db):
    with session_scope() as s:
        branch = shadow.create_branch(
            s, "acme/widget", _sid("acme/widget"), "line-a"
        )

    resp = _get_sync(
        _app(monkeypatch), f"/ui-api/pages/branches/{branch['id']}"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"] == []
    assert "has narrative notes" not in body["meta"]


def test_branch_thread_citation_to_real_decision(monkeypatch, clean_db):
    from snowline_governance import decisions

    with session_scope() as s:
        real = decisions.record_decision(
            s, "acme/widget", _sid("acme/widget"), "use postgres", "solid choice"
        )
        branch = shadow.create_branch(s, "acme/widget", _sid("acme/widget"), "line-a")
        node = shadow.add_node(s, "acme/widget", "line-a", "build on top of pg")
        shadow.add_citation(s, node["id"], cited_decision_id=real["id"])

    resp = _get_sync(
        _app(monkeypatch), f"/ui-api/pages/branches/{branch['id']}"
    )
    body = resp.json()
    assert body["nodes"][0]["citations"] == [f"decision:{real['id']}"]


def test_branch_thread_unknown_id_is_404_json(monkeypatch, clean_db):
    resp = _get_sync(
        _app(monkeypatch), f"/ui-api/pages/branches/{uuid.uuid4()}"
    )
    assert resp.status_code == 404
    assert "detail" in resp.json()


def test_branch_thread_malformed_id_is_404_json(monkeypatch, clean_db):
    resp = _get_sync(_app(monkeypatch), "/ui-api/pages/branches/not-a-uuid")
    assert resp.status_code == 404
    assert "detail" in resp.json()


# --- page: conversation merge + composer POST (shadow-conversations §5) ------


def test_branch_thread_merges_conversation_chronologically(monkeypatch, clean_db):
    # Separate commits give each row a distinct func.now() timestamp, so the
    # chronological interleave (node, message, node) this test asserts is
    # deterministic. Narrative notes must stay FIRST regardless.
    with session_scope() as s:
        branch = shadow.create_branch(
            s, "acme/widget", _sid("acme/widget"), "line-a", "the reasoning"
        )
    with session_scope() as s:
        shadow.add_node(s, "acme/widget", "line-a", "node one")
    with session_scope() as s:
        shadow.add_message(s, branch["id"], "a human reply", "human")
    with session_scope() as s:
        shadow.add_node(s, "acme/widget", "line-a", "node two")

    resp = _get_sync(_app(monkeypatch), f"/ui-api/pages/branches/{branch['id']}")
    assert resp.status_code == 200
    nodes = resp.json()["nodes"]

    # Narrative FIRST regardless of timestamp, then chronological interleave.
    assert [n["kind"] for n in nodes] == ["notes", "node", "message", "node"]
    assert nodes[0]["author"] == "narrative"
    assert nodes[1]["markdown"].startswith("node one")
    # A human message renders with the display author "you".
    assert nodes[2] == {
        "author": "you",
        "kind": "message",
        "markdown": "a human reply",
        "at": nodes[2]["at"],
    }
    assert nodes[3]["markdown"].startswith("node two")


def test_branch_thread_renders_agent_message_and_error(monkeypatch, clean_db):
    from snowline_governance.models import ShadowConversationEvent

    with session_scope() as s:
        branch = shadow.create_branch(
            s, "acme/widget", _sid("acme/widget"), "line-a"
        )
    with session_scope() as s:
        shadow.add_message(s, branch["id"], "an agent turn", "agent")
    with session_scope() as s:
        # An agent.error event (phase 2's turn-runner writes these; seeded here)
        # renders fail-visible as an "error" node.
        s.add(
            ShadowConversationEvent(
                branch_id=uuid.UUID(branch["id"]),
                seq=2,
                kind="agent.error",
                payload={"error": "codex timed out"},
            )
        )

    nodes = _get_sync(
        _app(monkeypatch), f"/ui-api/pages/branches/{branch['id']}"
    ).json()["nodes"]
    assert nodes[0] == {
        "author": "agent",
        "kind": "message",
        "markdown": "an agent turn",
        "at": nodes[0]["at"],
    }
    assert nodes[1] == {
        "author": "agent",
        "kind": "error",
        "markdown": "codex timed out",
        "at": nodes[1]["at"],
    }


def test_branch_thread_archived_sets_flags(monkeypatch, clean_db):
    with session_scope() as s:
        branch = shadow.create_branch(
            s, "acme/widget", _sid("acme/widget"), "line-a"
        )
    # Active: no flags.
    active = _get_sync(
        _app(monkeypatch), f"/ui-api/pages/branches/{branch['id']}"
    ).json()
    assert "flags" not in active or active["flags"] == []

    with session_scope() as s:
        shadow.archive_branch(s, "acme/widget", "line-a")

    archived = _get_sync(
        _app(monkeypatch), f"/ui-api/pages/branches/{branch['id']}"
    ).json()
    # The chosen archived-flag shape the shell's `disabled_when: "archived"` keys on.
    assert archived["flags"] == ["archived"]


def test_post_message_appends_and_returns_event(monkeypatch, clean_db):
    with session_scope() as s:
        branch = shadow.create_branch(
            s, "acme/widget", _sid("acme/widget"), "line-a"
        )

    resp = _post_sync(
        _app(monkeypatch),
        f"/ui-api/pages/branches/{branch['id']}/messages",
        {"markdown": "hello from the browser"},
    )
    assert resp.status_code == 200
    ev = resp.json()
    assert ev["seq"] == 1
    assert ev["kind"] == "message"
    # The route ALWAYS stamps author "human" — the browser is the human seam.
    assert ev["payload"] == {"author": "human", "markdown": "hello from the browser"}

    # It's durably appended — the thread page now shows it.
    nodes = _get_sync(
        _app(monkeypatch), f"/ui-api/pages/branches/{branch['id']}"
    ).json()["nodes"]
    assert nodes[-1]["markdown"] == "hello from the browser"
    assert nodes[-1]["author"] == "you"


def test_post_message_ignores_client_author(monkeypatch, clean_db):
    with session_scope() as s:
        branch = shadow.create_branch(
            s, "acme/widget", _sid("acme/widget"), "line-a"
        )
    # A client trying to spoof an agent author is ignored — the route forces human.
    resp = _post_sync(
        _app(monkeypatch),
        f"/ui-api/pages/branches/{branch['id']}/messages",
        {"markdown": "sneaky", "author": "agent"},
    )
    assert resp.status_code == 200
    assert resp.json()["payload"]["author"] == "human"


def test_post_message_unknown_branch_is_404(monkeypatch, clean_db):
    resp = _post_sync(
        _app(monkeypatch),
        f"/ui-api/pages/branches/{uuid.uuid4()}/messages",
        {"markdown": "hi"},
    )
    assert resp.status_code == 404


def test_post_message_malformed_branch_is_404(monkeypatch, clean_db):
    resp = _post_sync(
        _app(monkeypatch),
        "/ui-api/pages/branches/not-a-uuid/messages",
        {"markdown": "hi"},
    )
    assert resp.status_code == 404


def test_post_message_archived_branch_is_409(monkeypatch, clean_db):
    with session_scope() as s:
        branch = shadow.create_branch(
            s, "acme/widget", _sid("acme/widget"), "line-a"
        )
    with session_scope() as s:
        shadow.archive_branch(s, "acme/widget", "line-a")

    resp = _post_sync(
        _app(monkeypatch),
        f"/ui-api/pages/branches/{branch['id']}/messages",
        {"markdown": "too late"},
    )
    assert resp.status_code == 409


def test_post_message_blank_markdown_is_422(monkeypatch, clean_db):
    with session_scope() as s:
        branch = shadow.create_branch(
            s, "acme/widget", _sid("acme/widget"), "line-a"
        )
    resp = _post_sync(
        _app(monkeypatch),
        f"/ui-api/pages/branches/{branch['id']}/messages",
        {"markdown": "   "},
    )
    assert resp.status_code == 422


def test_post_message_oversize_markdown_is_422(monkeypatch, clean_db):
    from snowline_plugin_sdk.ui import UI_WRITE_BODY_LIMIT

    with session_scope() as s:
        branch = shadow.create_branch(
            s, "acme/widget", _sid("acme/widget"), "line-a"
        )
    resp = _post_sync(
        _app(monkeypatch),
        f"/ui-api/pages/branches/{branch['id']}/messages",
        {"markdown": "x" * (UI_WRITE_BODY_LIMIT + 1)},
    )
    assert resp.status_code == 422
