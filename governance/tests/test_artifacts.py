"""Artifact-graph behavior — register(inline)/get, revise/leaves, resolve
competing leaves, set_governs/set_maturity, the git-backend rejection, and the
ancestor-inherited `applicable_artifacts` against a STUBBED scope client.

DB-backed (skips cleanly when Postgres is unavailable). The scope dependency is
always a stub — these unit tests never require a running platform. Governs edges
key on the STABLE `scope_id` (#11), so the resolution maps the tests build use
the SAME per-slug id `StubScopeClient` serves.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import event

from snowline_governance import artifacts
from snowline_governance.db import get_engine


@contextlib.contextmanager
def _count_selects():
    """Count SELECT statements issued on the governance engine within the block —
    the fan-out metric issue #14 is about (the read paths under test do no
    writes). Yields a one-element list; read `counter[0]` after the block."""
    counter = [0]

    def _before(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter[0] += 1

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def _sid(slug: str) -> uuid.UUID:
    """The stable per-slug scope id — IDENTICAL to `StubScopeClient`'s, so a
    governs edge keyed on this id matches the chain rows the stub's `ancestors`
    serves (governs-matching keys on `scope_id`, not the mutable slug)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}")


def _scope_row(slug: str) -> dict:
    """The platform `to_row`-shaped resolution one slug resolves to — what the MCP
    surface passes into `resolved_scopes`."""
    return {"id": str(_sid(slug)), "slug": slug}


def _resolved(*slugs: str) -> dict[str, dict]:
    return {slug: _scope_row(slug) for slug in slugs}


def test_register_inline_then_get(db_session):
    art = artifacts.register_artifact(
        db_session, body="# spec body", doc_kind="spec", maturity="draft"
    )
    assert art["backend"] == "inline"
    assert art["doc_kind"] == "spec"
    assert art["maturity"] == "draft"
    assert art["version_count"] == 1
    assert art["is_branched"] is False
    assert art["current_version"]["has_snapshot"] is True
    assert art["governs"] == [] and art["governs_all"] is False

    got = artifacts.get_artifact(db_session, art["id"])
    assert got["id"] == art["id"]
    assert got["current_version"]["id"] == art["current_version"]["id"]
    # The canonical content is readable back (#132) — the full-record read
    # carries the current version's body by default.
    assert got["current_version"]["body_snapshot"] == "# spec body"


def test_get_artifact_body_shapes(db_session):
    """#132: `include_body=True` (the default) expands ONLY current_version;
    leaves stay lean; `include_body=False` restores the lean header shape; the
    WRITE returns stay lean (no body echo)."""
    art = artifacts.register_artifact(db_session, body="# v1")
    # Write return: lean — has_snapshot only, no body key.
    assert "body_snapshot" not in art["current_version"]

    got = artifacts.get_artifact(db_session, art["id"])
    assert got["current_version"]["body_snapshot"] == "# v1"
    # Leaves are lean headers even on the default read.
    assert all("body_snapshot" not in leaf for leaf in got["leaves"])

    lean = artifacts.get_artifact(db_session, art["id"], include_body=False)
    assert "body_snapshot" not in lean["current_version"]
    assert lean["current_version"]["has_snapshot"] is True


def test_get_artifact_version_reads_any_version_body(db_session):
    """#132: `get_artifact_version` serves the bodies `get_artifact` keeps lean —
    competing leaves (branch comparison) and superseded history (pinned reads)."""
    art = artifacts.register_artifact(db_session, body="root")
    root_v = art["current_version"]["id"]
    artifacts.revise_artifact(
        db_session, art["id"], relation="refines",
        supersedes=root_v, body_snapshot="branch A",
    )
    branched = artifacts.revise_artifact(
        db_session, art["id"], relation="pivot",
        supersedes=root_v, body_snapshot="branch B",
    )
    assert branched["is_branched"] is True

    # Every competing leaf's body is readable for comparison.
    bodies = {
        artifacts.get_artifact_version(db_session, art["id"], leaf["id"])[
            "body_snapshot"
        ]
        for leaf in branched["leaves"]
    }
    assert bodies == {"branch A", "branch B"}

    # The superseded root stays readable (audit / pinned exports), with lineage.
    root = artifacts.get_artifact_version(db_session, art["id"], root_v)
    assert root["body_snapshot"] == "root"
    assert root["artifact_id"] == art["id"]
    assert root["supersedes_id"] is None


def test_get_artifact_version_validates_pairing(db_session):
    a1 = artifacts.register_artifact(db_session, body="one")
    a2 = artifacts.register_artifact(db_session, body="two")
    with pytest.raises(ValueError, match="not a version id"):
        artifacts.get_artifact_version(db_session, a1["id"], "not-a-uuid")
    # A well-formed id matching NO version anywhere is a not-found, distinct
    # from the wrong-artifact pairing error (review finding on #136).
    with pytest.raises(ValueError, match="no version"):
        artifacts.get_artifact_version(db_session, a1["id"], str(uuid.uuid4()))
    with pytest.raises(ValueError, match="not a version of this artifact"):
        artifacts.get_artifact_version(
            db_session, a1["id"], a2["current_version"]["id"]
        )
    with pytest.raises(artifacts.ArtifactNotFoundError):
        artifacts.get_artifact_version(
            db_session, str(uuid.uuid4()), a1["current_version"]["id"]
        )


def test_register_inline_requires_body(db_session):
    with pytest.raises(ValueError, match="needs a body"):
        artifacts.register_artifact(db_session, body=None)
    # An empty body IS valid content (a deliberately-empty doc).
    art = artifacts.register_artifact(db_session, body="")
    assert art["current_version"]["has_snapshot"] is True


def test_git_backend_rejected(db_session):
    with pytest.raises(artifacts.GitBackendUnsupportedError):
        artifacts.register_artifact(db_session, body="x", backend="git")


def test_register_validates_enums(db_session):
    with pytest.raises(ValueError, match="doc_kind"):
        artifacts.register_artifact(db_session, body="x", doc_kind="bogus")
    with pytest.raises(ValueError, match="maturity"):
        artifacts.register_artifact(db_session, body="x", maturity="bogus")


def test_revise_changes_leaf(db_session):
    art = artifacts.register_artifact(db_session, body="v1")
    v1_id = art["current_version"]["id"]
    revised = artifacts.revise_artifact(
        db_session, art["id"], relation="refines",
        body_snapshot="v2", summary="tightened",
    )
    assert revised["version_count"] == 2
    assert revised["is_branched"] is False
    # The current leaf moved off v1 onto the new version.
    assert revised["current_version"]["id"] != v1_id
    assert revised["current_version"]["summary"] == "tightened"
    assert revised["current_version"]["relation"] == "refines"


def test_revise_invalid_relation_raises(db_session):
    art = artifacts.register_artifact(db_session, body="v1")
    with pytest.raises(ValueError, match="relation"):
        artifacts.revise_artifact(db_session, art["id"], relation="bogus")


def test_resolve_competing_leaves(db_session):
    art = artifacts.register_artifact(db_session, body="root")
    root_v = art["current_version"]["id"]
    # Two versions superseding the same root → two competing leaves.
    a = artifacts.revise_artifact(
        db_session, art["id"], relation="refines",
        supersedes=root_v, body_snapshot="branch A",
    )
    branched = artifacts.revise_artifact(
        db_session, art["id"], relation="pivot",
        supersedes=root_v, body_snapshot="branch B",
    )
    assert branched["is_branched"] is True
    leaf_ids = {v["id"] for v in branched["leaves"]}
    assert len(leaf_ids) == 2

    # Resolve passes the LOSING leaf's version id (flipped to superseded); the
    # OTHER leaf remains canonical.
    loser = next(iter(leaf_ids))
    winner = (leaf_ids - {loser}).pop()
    resolved = artifacts.resolve_artifact(db_session, art["id"], loser)
    assert resolved["is_branched"] is False
    assert {v["id"] for v in resolved["leaves"]} == {winner}
    # `a` is referenced so its branch participates in the competing set.
    assert a["id"] == art["id"]


def test_resolve_requires_competing_leaves(db_session):
    art = artifacts.register_artifact(db_session, body="solo")
    with pytest.raises(ValueError, match="single current leaf"):
        artifacts.resolve_artifact(
            db_session, art["id"], art["current_version"]["id"]
        )


def test_set_maturity(db_session):
    art = artifacts.register_artifact(db_session, body="x", maturity="draft")
    out = artifacts.set_maturity(db_session, art["id"], "exploratory")
    assert out["maturity"] == "exploratory"
    # Maturity is a descriptor, not a gate — any direction allowed, no version.
    back = artifacts.set_maturity(db_session, art["id"], "draft")
    assert back["maturity"] == "draft"
    assert back["version_count"] == 1
    with pytest.raises(ValueError, match="maturity"):
        artifacts.set_maturity(db_session, art["id"], "bogus")


def test_set_governs_keys_on_scope_id(db_session):
    art = artifacts.register_artifact(db_session, body="x")
    out = artifacts.set_governs(
        db_session, art["id"], "acme/widget",
        resolved_scopes=_resolved("acme/widget"),
    )
    assert out["governs"] == ["acme/widget"]
    assert out["governs_all"] is False

    # `*` sets governs_all and clears the rows (mutually exclusive).
    star = artifacts.set_governs(db_session, art["id"], "*")
    assert star["governs"] == [] and star["governs_all"] is True

    # None clears both.
    cleared = artifacts.set_governs(db_session, art["id"], None)
    assert cleared["governs"] == [] and cleared["governs_all"] is False


def test_set_governs_unresolved_slug_raises(db_session):
    art = artifacts.register_artifact(db_session, body="x")
    with pytest.raises(ValueError, match="not resolved"):
        artifacts.set_governs(
            db_session, art["id"], "acme/unknown", resolved_scopes={}
        )


def test_register_with_governs_list(db_session):
    art = artifacts.register_artifact(
        db_session, body="x",
        governs=["acme/widget", "acme/other"],
        resolved_scopes=_resolved("acme/widget", "acme/other"),
    )
    assert art["governs"] == ["acme/other", "acme/widget"]  # sorted


def test_list_artifacts_filter_by_governs(db_session, stub_scope_client):
    a1 = artifacts.register_artifact(
        db_session, body="governs widget",
        governs="acme/widget", resolved_scopes=_resolved("acme/widget"),
    )
    artifacts.register_artifact(
        db_session, body="governs other",
        governs="acme/other", resolved_scopes=_resolved("acme/other"),
    )
    # A governs_all artifact surfaces under any per-scope filter too.
    a3 = artifacts.register_artifact(db_session, body="everywhere", governs="*")

    out = artifacts.list_artifacts(
        db_session, governs="acme/widget", governs_scope_id=_sid("acme/widget")
    )
    ids = {a["id"] for a in out["artifacts"]}
    assert a1["id"] in ids and a3["id"] in ids
    assert out["items_total"] == 2

    # An unresolved governs scope yields an empty list (not an error).
    empty = artifacts.list_artifacts(
        db_session, governs="acme/missing", governs_scope_id=None
    )
    assert empty["artifacts"] == [] and empty["items_total"] == 0


def test_applicable_artifacts_inherits_ancestors(db_session, stub_scope_client):
    """`applicable_artifacts` returns own + ancestor-governing artifacts via the
    stub scope client, tagging inherited rows with `from_scope` and keying the
    governs match on the STABLE scope_id."""
    org = artifacts.register_artifact(
        db_session, body="org reference", doc_kind="reference",
        governs="acme", resolved_scopes=_resolved("acme"),
    )
    repo = artifacts.register_artifact(
        db_session, body="repo spec",
        governs="acme/widget", resolved_scopes=_resolved("acme/widget"),
    )
    feat = artifacts.register_artifact(
        db_session, body="feature plan", doc_kind="plan",
        governs="acme/widget/feat", resolved_scopes=_resolved("acme/widget/feat"),
    )
    everywhere = artifacts.register_artifact(db_session, body="conventions", governs="*")

    stub = stub_scope_client(
        tree={
            "acme": None,
            "acme/widget": "acme",
            "acme/widget/feat": "acme/widget",
        }
    )
    out = artifacts.applicable_artifacts(db_session, "acme/widget/feat", stub)
    assert stub.ancestors_calls == ["acme/widget/feat"]

    by_id = {a["id"]: a for a in out["artifacts"]}
    assert set(by_id) == {org["id"], repo["id"], feat["id"], everywhere["id"]}
    # Own-scope match carries NO from_scope; inherited rows carry the ancestor.
    assert "from_scope" not in by_id[feat["id"]]
    assert by_id[repo["id"]]["from_scope"] == "acme/widget"
    assert by_id[org["id"]]["from_scope"] == "acme"
    # governs_all is tagged from_scope='*'.
    assert by_id[everywhere["id"]]["from_scope"] == "*"


def test_applicable_artifacts_halts_at_isolated(db_session, stub_scope_client):
    """An isolated middle scope blocks governs inheritance from ABOVE it."""
    artifacts.register_artifact(
        db_session, body="org", governs="acme", resolved_scopes=_resolved("acme")
    )
    repo = artifacts.register_artifact(
        db_session, body="repo",
        governs="acme/widget", resolved_scopes=_resolved("acme/widget"),
    )
    feat = artifacts.register_artifact(
        db_session, body="feat",
        governs="acme/widget/feat", resolved_scopes=_resolved("acme/widget/feat"),
    )
    stub = stub_scope_client(
        tree={
            "acme": None,
            "acme/widget": "acme",
            "acme/widget/feat": "acme/widget",
        },
        isolated={"acme/widget"},  # blocks inheritance from acme
    )
    out = artifacts.applicable_artifacts(db_session, "acme/widget/feat", stub)
    got = {a["id"] for a in out["artifacts"]}
    assert got == {feat["id"], repo["id"]}  # org artifact NOT inherited


def _deep_artifact_tree(db_session, depth: int):
    """Register one artifact governing each of `depth` chained scopes
    acme/0/1/.../n-1, plus one `governs_all`. Returns (leaf_slug, tree) for a
    StubScopeClient — the ancestor chain from the leaf has length `depth`."""
    tree: dict[str, str | None] = {}
    parent: str | None = None
    slug = ""
    for i in range(depth):
        slug = "acme" if i == 0 else f"{slug}/{i}"
        artifacts.register_artifact(
            db_session, body=f"doc {i}", governs=slug,
            resolved_scopes=_resolved(slug),
        )
        tree[slug] = parent
        parent = slug
    artifacts.register_artifact(db_session, body="everywhere", governs="*")
    return slug, tree


def test_applicable_artifacts_is_batched(db_session, stub_scope_client):
    """Issue #14: `applicable_artifacts` must NOT scale its DB query count with
    the number of inherited artifacts (the prior per-item `session.get` +
    version/leaf/governs subqueries). A 3-deep and a 6-deep chain return the
    correctly merged + ordered + tagged result AND issue the SAME bounded number
    of SELECTs."""

    def run(depth: int) -> tuple[dict, int]:
        import sqlalchemy as sa

        db_session.execute(
            sa.text(
                "TRUNCATE artifacts, artifact_versions, artifact_governs "
                "RESTART IDENTITY CASCADE"
            )
        )
        leaf, tree = _deep_artifact_tree(db_session, depth)
        stub = stub_scope_client(tree=tree)
        with _count_selects() as counter:
            out = artifacts.applicable_artifacts(db_session, leaf, stub)
        return out, counter[0]

    shallow, shallow_q = run(3)
    deep, deep_q = run(6)

    # Behavior preserved: own-first (untagged), nearest-ancestor-next (tagged),
    # then governs_all tagged '*'; every level merged.
    deep_arts = deep["artifacts"]
    assert deep["items_total"] == 7  # 6 edge matches + 1 governs_all
    assert "from_scope" not in deep_arts[0]  # own scope
    assert deep_arts[-1]["from_scope"] == "*"  # governs_all last
    inherited_tags = [a.get("from_scope") for a in deep_arts[1:-1]]
    assert all(t is not None and t != "*" for t in inherited_tags)
    # Compact-row signals are intact (built from the batched fetch).
    assert all(a["version_count"] == 1 for a in deep_arts)
    assert all(a["is_branched"] is False for a in deep_arts)

    # The query count does NOT grow with the inherited-artifact count (#14).
    assert shallow_q == deep_q


def _git_reference(db_session, *, repo: str, path: str, governs_slug: str):
    """Construct a git-backed `reference` artifact governing `governs_slug`,
    directly on the session — the PRODUCTION-FAITHFUL shape (#43/#40 lesson).

    The migrated org reference docs (turtlesedge's `brand/guidelines.yaml`,
    `TONE.md`) are `backend='git'` with `repo`/`path` set and a governs edge to
    the ORG. `register_artifact` is inline-only (rejects git), so a
    production-shaped fixture builds the row + version + governs edge itself,
    keyed on the SAME stable `scope_id` the stub/`HttpScopeClient` chain serves."""
    from snowline_governance.models import (
        Artifact,
        ArtifactGoverns,
        ArtifactVersion,
    )

    art = Artifact(doc_kind="reference", backend="git", repo=repo, path=path)
    db_session.add(art)
    db_session.flush()
    db_session.add(ArtifactVersion(artifact_id=art.id, body_snapshot=None))
    db_session.add(
        ArtifactGoverns(
            artifact_id=art.id,
            scope_id=_sid(governs_slug),
            scope_slug=governs_slug,
        )
    )
    db_session.flush()
    return art


def test_applicable_artifacts_inherited_row_carries_repo_path(
    db_session, stub_scope_client
):
    """The #44 case, production-faithful: an org-registered git-backed reference
    doc (repo/path set, `governs`=org) is inherited by a child repo scope, TAGGED
    `from_scope`=org AND carrying its human-readable `repo`/`path` identity — the
    field the live rows lacked, which made #44's verification "needlessly blind".
    Mirrors the live turtlesedge→turtletracks shape.

    The child's OWN inline spec carries `repo`/`path` KEYS too (None — an inline
    substrate doc has no repo path), so a consumer always finds the fields."""
    org_ref = _git_reference(
        db_session, repo="Org/org-brand", path="brand/guidelines.yaml",
        governs_slug="org",
    )
    own = artifacts.register_artifact(
        db_session, body="repo spec",
        governs="org/repo", resolved_scopes=_resolved("org/repo"),
    )

    stub = stub_scope_client(tree={"org": None, "org/repo": "org"})
    out = artifacts.applicable_artifacts(db_session, "org/repo", stub)
    by_id = {a["id"]: a for a in out["artifacts"]}

    inherited = by_id[str(org_ref.id)]
    assert inherited["from_scope"] == "org"  # org-inherited, not own
    assert inherited["repo"] == "Org/org-brand"  # human-readable identity...
    assert inherited["path"] == "brand/guidelines.yaml"  # ...the live rows lacked
    assert inherited["backend"] == "git"

    own_row = by_id[own["id"]]
    assert "from_scope" not in own_row  # own scope
    assert own_row["repo"] is None and own_row["path"] is None  # keys present


def test_applicable_artifacts_over_real_http_transport(db_session):
    """Real-transport inheritance case (#44): drive `applicable_artifacts`
    end-to-end through the REAL `HttpScopeClient` over a genuine httpx round-trip
    whose response is the platform's actual `GET /scopes/{slug}/ancestors` JSON
    contract (`{"ancestors": [<to_row>, ...]}`, isolation-halting, org last) —
    proving the walk composes with real HTTP + JSON parse, not just the in-memory
    stub. Mirrors the org→repo shape #44 was found against."""
    import httpx

    from snowline_governance.scope_client import HttpScopeClient

    org_id, repo_id = _sid("org"), _sid("org/repo")

    def _row(slug, sid):
        return {
            "id": str(sid), "slug": slug, "name": slug,
            "kind": "org" if "/" not in slug else "project",
            "status": "active", "isolated": False,
            "org": slug.split("/", 1)[0],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/scopes/org/repo/ancestors"
        return httpx.Response(
            200,
            json={"ancestors": [_row("org/repo", repo_id), _row("org", org_id)]},
        )

    client = HttpScopeClient(
        "http://platform.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    org_ref = _git_reference(
        db_session, repo="Org/org-brand", path="TONE.md", governs_slug="org",
    )

    out = artifacts.applicable_artifacts(db_session, "org/repo", client)
    by_id = {a["id"]: a for a in out["artifacts"]}
    assert str(org_ref.id) in by_id  # org reference inherited over the wire
    inherited = by_id[str(org_ref.id)]
    assert inherited["from_scope"] == "org"
    assert inherited["path"] == "TONE.md"
