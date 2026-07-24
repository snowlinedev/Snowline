"""Governance test harness: a disposable Postgres DB, schema built via Alembic.

Mirrors the platform's `tests/conftest.py`. The governance test database is
created from scratch, migrated with `alembic upgrade head` (so the migration
chain is exercised every run), and each test gets a clean `decisions` table via
TRUNCATE.

If Postgres is unreachable, the DB-backed fixtures `pytest.skip` with a clear
message rather than erroring — the import-purity / registration / stub-based
tests that don't need the DB still run.
"""

import os
import uuid
from pathlib import Path

import pytest

# Point governance's DB layer at the disposable test database BEFORE any
# governance module builds its (lazy) engine.
TEST_DB_URL = os.environ.get(
    "SNOWLINE_GOVERNANCE_TEST_DATABASE_URL",
    "postgresql+psycopg:///snowline_governance_test",
)
os.environ["SNOWLINE_GOVERNANCE_DATABASE_URL"] = TEST_DB_URL

import sqlalchemy as sa  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from snowline_governance.milestone_client import (  # noqa: E402
    MilestoneResolutionError,
    MilestoneServiceError,
)
from snowline_governance.scope_client import ScopeNotFoundError  # noqa: E402

MIGRATIONS = (
    Path(__file__).parents[1] / "src" / "snowline_governance" / "migrations"
)


@pytest.fixture(autouse=True)
def _shadow_turns_stay_off(monkeypatch):
    """The turn-runner must NEVER start inside the suite: full-lifespan tests
    (test_app_http, test_ui_api) enter the real app lifespan, and a dev shell's
    `export SNOWLINE_SHADOW_TURNS_ENABLED=1` (natural while working on #71)
    would otherwise spawn real codex subprocesses against seeded branches
    mid-test. Symmetric to how those tests pin SNOWLINE_WEBHOOK_DISABLED."""
    monkeypatch.setenv("SNOWLINE_SHADOW_TURNS_ENABLED", "0")


@pytest.fixture(autouse=True)
def _replication_delivery_stays_off(monkeypatch):
    """The SDK replication delivery loop (app lifespan, #79) must not tick
    inside the suite — same rationale as the webhook/turn pins. The tests that
    exercise delivery call `deliver_pending` directly, which this flag does not
    gate."""
    monkeypatch.setenv("SNOWLINE_REPLICATION_DISABLED", "1")


def _db_name(url: str) -> str:
    return sa.make_url(url).database


def _maintenance_url(url: str) -> str:
    # render_as_string, NOT str(): str() masks the password as `***`, which the
    # maintenance engine would then send literally — a password-bearing test DB
    # URL could never connect.
    return sa.make_url(url).set(database="postgres").render_as_string(
        hide_password=False
    )


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option(
        "sqlalchemy.url", os.environ["SNOWLINE_GOVERNANCE_DATABASE_URL"]
    )
    return cfg


def _postgres_reachable() -> bool:
    try:
        eng = sa.create_engine(
            _maintenance_url(TEST_DB_URL), isolation_level="AUTOCOMMIT"
        )
        with eng.connect():
            pass
        eng.dispose()
        return True
    except Exception:
        return False


def create_database(url: str) -> None:
    name = _db_name(url)
    eng = sa.create_engine(_maintenance_url(url), isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        exists = conn.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        ).scalar()
        if not exists:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    eng.dispose()


def drop_database(url: str) -> None:
    name = _db_name(url)
    eng = sa.create_engine(_maintenance_url(url), isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ),
            {"n": name},
        )
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
    eng.dispose()


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """A freshly created + migrated governance test database for the session."""
    if not _postgres_reachable():
        pytest.skip(
            "Postgres not reachable at "
            f"{_maintenance_url(TEST_DB_URL)!r} — DB-backed tests skipped"
        )
    from snowline_governance.db import reset_engine

    drop_database(TEST_DB_URL)
    create_database(TEST_DB_URL)
    reset_engine()
    command.upgrade(alembic_config(), "head")
    yield TEST_DB_URL
    reset_engine()
    drop_database(TEST_DB_URL)


@pytest.fixture()
def clean_db(migrated_db):
    """Truncate the governance tables before each test (so writes a test makes are
    visible across the separate sessions a tool opens — mirroring production).
    Lists every root table explicitly; CASCADE clears the dependent version/govern
    rows."""
    import sqlalchemy as sa

    from snowline_governance.db import session_scope

    with session_scope() as s:
        s.execute(
            sa.text(
                "TRUNCATE decisions, artifacts, artifact_versions, "
                "artifact_governs, shadow_branches, shadow_nodes, "
                "shadow_node_citations, shadow_conversation_events, "
                "webhook_subscriptions, webhook_deliveries, "
                "decision_concurrences, replication_lww_registers, "
                "replication_subscriptions, replication_outbox, "
                "replication_stream_counters, replication_inbound_streams, "
                "replication_parked_events "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture()
def db_session(clean_db):
    """A clean ORM session per test (auto-commit on success)."""
    from snowline_governance.db import session_scope

    with session_scope() as s:
        yield s


# --- a stub ScopeClient so unit tests need no running platform --------------


class StubScopeClient:
    """An in-memory `ScopeClient` double (governance-plugin spec: applicability
    must be testable without a live platform).

    `tree` maps slug -> parent slug (None for a root). `isolated` is the set of
    slugs that block inheritance from above. It computes `ancestors(slug)` with
    the SAME isolation-halting rule the platform's scope service implements (own
    scope first, climb to parent, stop at the first `isolated` node + the root)
    and records every slug it was asked about — so a test can assert governance
    queried the chain and merged it. `_row` matches the platform's `to_row`
    shape (incl. `id`, the soft reference governance stores).
    """

    def __init__(
        self,
        tree: dict[str, str | None],
        isolated: set[str] | None = None,
    ) -> None:
        self._tree = tree
        self._isolated = isolated or set()
        self._ids = {
            slug: uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{slug}") for slug in tree
        }
        self.ancestors_calls: list[str] = []
        self.resolve_calls: list[str] = []

    def _row(self, slug: str) -> dict:
        return {
            "id": str(self._ids[slug]),
            "slug": slug,
            "name": slug,
            "kind": "org" if "/" not in slug else "project",
            "status": "active",
            "isolated": slug in self._isolated,
            "org": slug.split("/", 1)[0],
        }

    def resolve(self, slug: str) -> dict | None:
        self.resolve_calls.append(slug)
        return self._row(slug) if slug in self._tree else None

    def ancestors(self, slug: str) -> list[dict]:
        self.ancestors_calls.append(slug)
        if slug not in self._tree:
            # Mirror HttpScopeClient: the platform 404s an unknown slug. An
            # empty-chain return here would let a caller silently treat an
            # unknown scope as "no ancestors" instead of the §8 retryable
            # error the real client raises.
            raise ScopeNotFoundError(f"no scope with slug {slug!r} (stub 404)")
        chain: list[dict] = []
        seen: set[str] = set()
        node: str | None = slug
        while node is not None and node in self._tree and node not in seen:
            seen.add(node)
            chain.append(self._row(node))
            if node in self._isolated:  # blocks inheritance from ABOVE
                break
            node = self._tree[node]
        return chain


@pytest.fixture()
def stub_scope_client():
    """Factory: `stub_scope_client(tree, isolated=...)` -> `StubScopeClient`."""
    return StubScopeClient


# --- a stub MilestoneClient so unit tests need no running platform ----------


class StubMilestoneClient:
    """An in-memory `MilestoneClient` double (milestones.md §6.1: canonicality
    must be testable without a live platform).

    `statuses` maps a canonical milestone ADDRESS (`anchor/name`) to its status
    (`planned`/`active`/`achieved`/`cancelled`); mutate one with `set_status` to
    model a platform transition (the marquee promotion/demotion test). `aliases`
    maps a TARGET address to the tombstone slugs that resolve to it — a stored
    stamp is matched against `{target} ∪ aliases` (§5).

    It mirrors the platform's resolution semantics closely enough for the
    consumer: a full address resolves directly; a BARE name resolves against a
    `context` scope by trying the repo anchor (`context/name`) then the org
    anchor (`org/name`); a miss raises `MilestoneResolutionError` carrying
    same-named `suggestions` (never an automatic resolution). It records every
    call so a test can assert the read did ONE `resolve_batch` (§6.1.2). Set
    `raise_transport=True` to model a platform outage — `resolve_batch` then
    raises `MilestoneServiceError` (the HARD read error).
    """

    def __init__(
        self,
        statuses: dict[str, str] | None = None,
        aliases: dict[str, list[str]] | None = None,
    ) -> None:
        self._statuses = {self._fold(k): v for k, v in (statuses or {}).items()}
        self._aliases = {
            self._fold(t): [self._fold(a) for a in al]
            for t, al in (aliases or {}).items()
        }
        self._alias_to_target: dict[str, str] = {}
        for tgt, al in self._aliases.items():
            for a in al:
                self._alias_to_target[a] = tgt
        self.resolve_calls: list[tuple[str, str | None]] = []
        self.batch_calls: list[list[str]] = []
        self.aliases_calls: list[str] = []
        self.raise_transport = False

    @staticmethod
    def _fold(s):
        return s.strip().lower() if isinstance(s, str) else s

    def set_status(self, address: str, status: str) -> None:
        self._statuses[self._fold(address)] = status

    def _candidates(self, ref: str, context: str | None) -> list[str]:
        ref = self._fold(ref)
        if "/" in ref:
            return [ref]
        if context is None:
            return []
        ctx = self._fold(context)
        cands = [f"{ctx}/{ref}"]
        org = ctx.split("/", 1)[0]
        if org != ctx:
            cands.append(f"{org}/{ref}")
        return cands

    def _suggestions(self, ref: str) -> list[dict]:
        name = self._fold(ref).rsplit("/", 1)[-1]
        return [
            {"address": a, "status": s}
            for a, s in sorted(self._statuses.items())
            if a.rsplit("/", 1)[-1] == name
        ]

    def _resolve_address(self, ref, context):
        for cand in self._candidates(ref, context):
            if cand in self._alias_to_target:
                tgt = self._alias_to_target[cand]
                return (tgt, self._statuses.get(tgt), True)
            if cand in self._statuses:
                return (cand, self._statuses[cand], False)
        return None

    def resolve(self, ref: str, context: str | None = None) -> dict:
        self.resolve_calls.append((ref, context))
        hit = self._resolve_address(ref, context)
        if hit is None:
            raise MilestoneResolutionError(
                f"unknown milestone {ref!r}", self._suggestions(ref)
            )
        addr, status, via = hit
        return {"address": addr, "status": status, "resolved_via_alias": via}

    def resolve_batch(self, refs, context: str | None = None) -> dict[str, dict]:
        self.batch_calls.append(list(refs))
        if self.raise_transport:
            raise MilestoneServiceError("stub milestone transport failure")
        out: dict[str, dict] = {}
        for ref in refs:
            hit = self._resolve_address(ref, context)
            if hit is None:
                out[ref] = {
                    "error": f"unknown milestone {ref!r}",
                    "suggestions": self._suggestions(ref),
                }
            else:
                addr, status, via = hit
                out[ref] = {
                    "address": addr,
                    "status": status,
                    "resolved_via_alias": via,
                }
        return out

    def aliases(self, address: str) -> dict:
        self.aliases_calls.append(address)
        hit = self._resolve_address(address, None)
        target = hit[0] if hit else self._fold(address)
        return {"target": target, "aliases": list(self._aliases.get(target, []))}


@pytest.fixture()
def stub_milestone_client():
    """Factory: `stub_milestone_client(statuses, aliases=...)` ->
    `StubMilestoneClient`."""
    return StubMilestoneClient
