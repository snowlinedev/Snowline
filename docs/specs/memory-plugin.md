# Memory plugin

> **Status: draft.** The functional contract for Snowline's session-memory
> plugin (governance decision `a45b2734`, Sean 2026-07-02). It describes EXPECTED
> FUNCTIONALITY and could be handed to a fresh agent. Same shape as the
> governance plugin: a workspace member with its own DB, a FastMCP surface mapped
> onto `main`, self-registering with the platform.

## 1. Purpose

Memory is Snowline's **cross-folder, cross-machine agent session memory**: the
durable working context a session needs to be productive — project conventions,
gotchas, user preferences, the current focus, useful references.

The base Claude Code memory system keys recall to the working **folder**. After
the monolith→platform cutover the project's working memory sits locked to one
checkout, penalizing sessions in the repos where work actually happens. Memory
belongs behind the gateway like every other capability: reachable from any
folder, any machine, scope-aware.

Memory does **not** own scopes — the **platform** owns the scope namespace.
Memory stores a scope **slug** as a soft, optional reference (never a
cross-service join), so it can filter/tag memories by scope without a platform
round-trip.

## 2. Place in the platform

Memory is a plugin: an out-of-process module the platform's gateway composes and
the supervisor health-checks. It:

- **Registers** with the platform via a manifest declaring its `base_url`, its
  MCP surface, and its health endpoint (`{"/mcp": "main"}` — one surface, mapped
  onto `main`). Registration is best-effort and single-shot per boot — a
  briefly-down platform can't crash the plugin (architecture §3, hot-pluggable);
  a failed registration is re-attempted on restart (the standing heartbeat/retry
  design is platform#39).
- **Maps one MCP surface onto `main`.** The verbs live alongside governance's on
  the composed `main` surface a client sees. Memory has no isolated (`shadow`)
  surface — every verb is a first-class working-memory op.
- **Has its own database** — a separate Postgres DB from the platform's and from
  governance's. It boot-migrates to the latest Alembic head in the lifespan (a
  schema change deploys on a plain restart).

## 3. The governance-vs-memory boundary

This is the load-bearing distinction, and it is deliberate:

| | Governance | Memory |
|---|---|---|
| **What** | ratified reasoning — *what was decided and why* | working context — *what a session needs to know to be productive* |
| **Lifecycle** | permanent, superseded (never deleted); a supersession DAG | mutable working notes; upserted in place; `forget`-able |
| **Authority** | policy — decisions bind future work | advisory — hints, conventions, preferences |
| **Read surface** | `applicable_decisions` (ancestor-inherited, isolation-aware) | `memory_digest` / `recall` (scope + portfolio-wide, flat) |

**A memory that hardens into policy graduates — it does not live in memory
forever.** When a piece of working context becomes a ratified rule ("we always do
X"), the right move is `record_decision` on the governance surface, then `forget`
the memory (or leave it as the human-readable hook). Memory is the on-ramp;
governance is the record. Memory intentionally has **no** supersession graph, no
inheritance walk, no events — those are governance's job. Memory is a flat,
scope-tagged, upsert-in-place key/value-ish store optimized for the
session-start read.

## 4. Data model

One table, `memories`:

- `id` — uuid PK.
- `name` — text, **UNIQUE NOT NULL**, kebab-case (`^[a-z0-9]+(-[a-z0-9]+)*$`).
  The stable upsert key. Generated from the description/content head when a
  caller omits it. A caller-**provided** name is **auto-normalized** to this
  form rather than rejected — lowercased, with underscores/spaces/other
  invalid-character runs collapsed to a single hyphen and clamped to the max
  length of 80 chars (`my_note` / `My Note Title` both save on the first
  attempt). A whitespace-only name counts as omitted. Only an
  all-punctuation name that normalizes to `""` raises. `forget` normalizes its
  `name` argument the same way, so a name saved as `my_note` (stored as
  `my-note`) can still be forgotten by either spelling.
- `description` — text NOT NULL. The **one-line hook** shown in the digest
  (`name — description`). Derived from the content's first line when omitted.
- `content` — text NOT NULL. The markdown body — the actual working note.
- `kind` — text NOT NULL, a **soft enum**: `user` / `feedback` / `project` /
  `reference` / `gotcha` (unknown values are stored verbatim, defaulting to
  `project`). Soft, so a new kind never needs a migration. Lowercased at the
  `remember` boundary (`Gotcha` ≡ `gotcha`) so case can't split the digest.
- `scope_slug` — text NULL. A **soft** scope reference, validated against the
  platform slug grammar when present, stored **verbatim** (never resolved to a
  platform id, never a join). NULL ⇒ portfolio-wide (applies everywhere).
- `created_at` / `updated_at` — timestamps; `updated_at` bumps on **every**
  upsert, including an identical-content re-`remember` (the write is a
  deliberate touch — recency reflects the last time the note was asserted).

### Full-text search

**Choice: a Postgres-native generated `tsvector` column + a GIN index** (the
issue's first option, mirroring the monolith's stored-column search). The column
is `search_vector`, `GENERATED ALWAYS AS to_tsvector('english', name ‖
description ‖ content) STORED`, indexed with GIN. `recall` matches it with
`websearch_to_tsquery` and ranks with `ts_rank`. Rationale for the stored column
over inline `to_tsvector` even at ~40 rows: it's the idiomatic Postgres pattern,
it keeps the query trivial (`search_vector @@ q`), and it costs one cheap
generated column — there is no downside at this size and it scales for free.

## 5. MCP surface (mapped onto `main`)

All verbs run their blocking DB work in a thread (the monolith's
`anyio.to_thread.run_sync` pattern) so the async transport isn't blocked.

- **`remember(content, name?, description?, kind?, scope?)`** — the write.
  **Upsert by `name`**: writing the same (normalized) name updates the row in
  place (bumping `updated_at`); a new name inserts. When `name` is provided it's
  **auto-normalized** to kebab-case (lowercased, invalid-character runs → single
  hyphens, clamped) rather than rejected — it only raises if normalization
  leaves nothing. When `name` is omitted it's generated (kebab) from the
  description, else the content head. When `description` is omitted it's
  derived from the content's first line. `kind` defaults to `project` and is
  lowercased at the boundary; `scope` is
  an optional slug (validated, stored verbatim). Save durable working context —
  conventions, gotchas, preferences, current focus — **not** things the repo/git
  already record.
- **`recall(query?, kind?, scope?, limit=10)`** — the search. With `query`:
  FTS-ranked (`websearch_to_tsquery` + `ts_rank`) over name+description+content.
  Without: newest-first. `kind` filters exactly; `scope` returns that scope's
  rows **plus** portfolio-wide (`scope_slug IS NULL`) rows. Returns the matching
  rows + `items_total`.
- **`memory_digest(scope?)`** — the **session-start read**. ALL memories as
  one-line entries (`name — description`), grouped by kind, cheap and
  deterministic (no FTS, no ranking). With `scope`: that scope's rows plus
  portfolio-wide rows; without: everything. This is the compensation for the
  harness not auto-injecting memory — call it at the top of any session.
- **`list_memories(kind?, scope?, limit?)`** — hygiene/browse. Headers (name,
  description, kind, scope, updated_at), newest-first, filtered like `recall`.
- **`forget(name)`** — delete one memory by name. Idempotent (a no-op miss
  reports `forgotten: false`).

## 6. Privacy

**The code is public; the content is private.** This plugin's source lives in the
public platform repo. The `memories` table lives in the owner's own Postgres DB
on the owner's own box — the working notes (which may name people, repos,
preferences, unreleased plans) never enter the public repo and, for a
cross-tailnet deployment, never touch the public host. This mirrors the platform
rule that private plugins run on the owner's machine and register over the
tailnet (architecture §3).

## 7. Importer

`scripts/import_claude_memories.py` ingests the existing Claude Code memory
markdown files (`~/.claude/projects/*/memory/*.md`): frontmatter `name` /
`description` / `metadata.type` (mapped `type → kind`, unknown → `project`) + the
markdown body after the frontmatter as `content`. Folded/literal YAML block
scalars in the frontmatter (`description: >-` …) are joined/preserved — the bare
indicator is never stored. It **upserts** via the same `remember` semantics
(idempotent — re-running updates in place, never duplicates). Records are
**validated at parse time** (name/scope grammar, kind normalization — the same
checks `remember` applies), so `--dry-run` predicts live outcomes and previews
each file's parsed name/kind/description. Two files whose names normalize to
the **same key** (`My_Note.md` + `my-note.md`) would silently last-write-win
through the upsert, so the batch is deduped at parse time: the first file (in
sorted order) keeps the name and later colliders are reported `failed` naming
the winner — in dry-run and live identically. Live application is **per-file
isolated** (a savepoint per record): one bad file is reported `failed` with its
reason and the rest still import; the per-file report always prints and the exit
code is nonzero when any file failed. ~40 memories migrate on day one. The
orchestrator runs it at deploy against the live store; it is never run
automatically.

## 8. Out of scope

- Inheritance / ancestor walks / isolation — memory is flat and scope-tagged;
  the isolation-aware walk is governance's.
- Supersession / decision events / a webhook bus — a hardened memory *graduates*
  to `record_decision` (§3), it does not accumulate a lineage here.
- Scopes — owned by the platform; memory references a slug as a soft, optional,
  never-resolved reference.

## 9. Acceptance criteria

- Registers with the platform and serves `remember` / `recall` / `memory_digest`
  / `list_memories` / `forget` on `/mcp` (mapped onto `main`), reachable over the
  real streamable-HTTP transport at exactly `/mcp`.
- `remember` upserts by name (same name updates in place), validates kebab names,
  generates a name when omitted, derives a description when omitted.
- `recall` ranks by FTS when a query is present, newest-first otherwise, and
  filters by kind + scope (scope ⊇ portfolio-wide).
- `memory_digest` returns all applicable memories as one-line entries grouped by
  kind, scope-filtered plus portfolio-wide.
- The importer round-trips fixture markdown files idempotently.
- Imports no platform internals / no monolith code (import-pure); runs against
  its own DB.
