# Scope namespace

> **Status: draft.** The platform-owned shared spine: the `Scope` identity +
> `parent_id` tree + `isolated` flag, plus the read/resolve surface plugins
> consume. The functional contract for issue #6. Read `architecture.md` first.

## 1. Purpose

Scopes are the namespace every capability hangs off — a decision, a work item, an
artifact, a repo all live "at a scope." The **platform owns scopes** so all
plugins share one tree, and **isolation + inheritance are properties of that
tree**. Plugins reference scopes by **slug** (soft references) and read the tree
from the platform; they never own it.

## 2. The Scope model (schema-compatible with the monolith)

- `slug` — unique, the namespace key; **hierarchical** (`org`, `org/repo`,
  `org/repo/initiative`).
- `name` — display name.
- `kind` — `org` | `project` | `initiative` | `component` | … . **Invariant:** a
  bare slug (no `/`) ⇔ `kind == org`.
- `parent_id` — self-FK; the **authoritative** tree edge (not the slug prefix);
  kept consistent with the slug hierarchy.
- `isolated` — bool; the **inheritance boundary** (governance/artifacts do not
  inherit *across* an isolated node).
- `status` — `active` | `stub` | … .
- `created_at` / `updated_at`.

Schema stays **compatible with the monolith's `scopes` table** so existing scopes
migrate into the running instance later (a straight import).

## 3. Operations (the scope service)

- `resolve(slug) -> Scope | None` — non-mutating lookup.
- `ancestors(scope) -> [scope, parent, …]` — nearest-first, **HALTING at the
  first `isolated` node and the forest root**. This is the applicability walk
  governance needs; a visited-guard makes a malformed `parent_id` cycle terminate.
- `tree(root=None) -> nested forest` — `parent_id`-edged, slug-ordered, exposing
  `isolated` on each node. A `root` slug returns that subtree; omitted → the whole
  forest (dangling/None parents are forest roots, so nothing is dropped).
- `list(org=None) -> flat rows`.
- `create(slug, name, kind, parent=None, isolated=False)` / `update(...)` —
  enforces the bare-slug⇔org invariant and derives/validates `parent_id` from the
  slug hierarchy on create.

**Auto-vivify:** `resolve` is **non-mutating** (returns `None` for unknown) — no
implicit stub creation in the public read path; creation is explicit via
`create`. (The monolith's `resolve_or_stub` convenience is intentionally *not*
carried into the platform's read surface.)

## 4. Surfaces

- **Read/resolve HTTP API** (for out-of-process plugins like governance — they
  fetch the tree over HTTP, since a plugin can't import the platform): `GET
  /scopes`, `GET /scopes/{slug}`, `GET /scopes/tree` (`?root=`), `GET
  /scopes/{slug}/ancestors`. Behind the platform trust gate.
- **Scope MCP tools on the platform's `main` surface** (for the agent):
  `list_scopes`, `scope_tree`, `resolve_scope`, `create_scope` / `update_scope`.
  Thin wrappers over the service. *(First cut may land the read API + service and
  a minimal tool set; full write tooling can follow.)*

## 5. Behaviors (carried from the monolith, functionality-first)

- The **isolation-aware ancestor walk** — mirror `graph.ancestor_scopes_until_isolated`:
  own scope first, climb `parent_id`, stop at the first `isolated` node (it is
  included; its parent is not) and at the root.
- The **nested tree builder** — mirror `graph.scope_tree`: forest of `parent_id`
  children, slug-ordered, `isolated` on every node, cycle-safe.
- `parent_id` is authoritative; the slug hierarchy and `parent_id` are kept
  consistent on create (parent derived from the slug's prefix unless given).

## 6. Out of scope

- Plugin data that *references* scopes (decisions, work items, artifacts) — those
  live in their plugins.
- Migrating existing monolith scope data — a later step once the platform runs;
  the schema is kept compatible so it's a straight import.

## 7. Implementation note (carve from the frozen monolith)

Carve the model + walk + tree from the monolith (read-only reference at
`/Users/seanlynch/Projects/Snowline/snowline`): `models_core.Scope` (minus plugin-domain
freight), and `graph.py`'s `get_scope` / `ancestor_scopes_until_isolated` /
`scope_tree`. Stand up the platform's **DB layer** — Postgres + SQLAlchemy +
Alembic, mirroring the monolith's `substrate/db.py` + alembic setup — since
scopes are the platform's first persisted data. Import-pure (no monolith imports).

## 8. Acceptance criteria

- `Scope` model + an Alembic migration; the `scopes` schema is compatible with
  the monolith's (an existing dump's `scopes` rows import cleanly).
- `resolve` / `list` / `tree` / `ancestors` work; the ancestor walk halts at the
  first `isolated` node (verified with a multi-level tree incl. an isolated
  middle node); `tree` exposes `isolated`.
- A read/resolve HTTP API behind the trust gate; a test fetches a scope's
  ancestor chain over it.
- `create` enforces bare-slug⇔org and keeps `parent_id` consistent with the slug
  hierarchy.
- Tests pass against a Postgres test DB; the package imports no monolith code.
