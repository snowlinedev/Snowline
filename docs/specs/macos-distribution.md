# macOS distribution — the packaged stable channel (installable-v1)

> **Status: draft.** Design for milestone `snowlinedev/installable-v1`
> (issue #201): the MacBook Pro instance is installed and updated from
> **packaged stable builds**, never a source checkout. This spec makes the
> five calls the implementation items build against: artifact format,
> Postgres strategy, version scheme, update/rollback path, and the v1
> component set with each component's data story. Companion specs:
> `replication-continuity.md` (§5 spoke posture, §7 seeding),
> `docs/ops/roam-runbook.md` (the operational drill this channel automates),
> `deploy-continuity.md` (restart visibility). Dashboard dist resolution is
> `config.dashboard_dist()` in the platform source (`SNOWLINE_DASHBOARD_DIST`
> override + checkout-relative fallback).

## 1. Scope, precisely

**In scope:** installing, updating, and rolling back the full local Snowline
instance on the MBP — platform (gateway + dashboard + CLI), governance,
memory, and pm — as a **spoke** of the always-on primary, from versioned
release artifacts. Single operator (Sean), machines on the shared tailnet,
`gh` authenticated.

**Out of scope, explicitly:**

- **The primary (mini) stays source-run.** Its checkout + `git pull` +
  kickstart flow is unchanged. Migrating the hub onto the packaged channel is
  a future item, gated on the channel proving itself on the spoke through a
  few train cycles.
- **Generic OSS self-host.** The abandoned oss-release phase-2 path
  (docker-compose 489979b8, cross-platform secrets f09afcd5, operator docs
  3b85c648) aimed at strangers on arbitrary platforms. This channel is
  macOS-native and assumes the operator's own `gh` auth (the pm repo is
  private). Mining, not duplicating: the degradation-matrix and env-surface
  thinking from those items informs §6's config templates.
- **walkthrough-mcp, musher, remote-front** — §7 gives the per-component
  rationale.

**Correction to the framing in #201:** the item body says "governance/memory
… have no replication". They do — governance (#79) and memory (#80) shipped
full replication streams (governance carries its own `replication_stream.py`
+ `replication_apply.py`; memory's rides `snowline_plugin_sdk.replication`
with its apply seam inside `memory.py`), and the roam runbook already stands
both up as spoke services. The
open data-story questions narrow to walkthrough and musher (§7).

## 2. Decision D1 — artifact format: wheels on per-repo GitHub Releases, installed by uv into managed venvs

Each release ships **Python wheels** built by the release cutter (§4 —
host-side, not CI) and attached to a GitHub Release on the repo that owns
the code:

- `snowlinedev/Snowline` release: wheels for `snowline-platform`,
  `snowline-governance`, `snowline-memory`, `snowline-plugin-sdk` — built
  from the workspace, but note `uv build --all-packages` also produces
  `snowline-remote-front` (a workspace member): the pipeline builds
  per-package or prunes that wheel; it never ships in this release. Plus
  three non-wheel assets: `dashboard-dist-<v>.tar.gz` (§2.2), `train.json`
  (§4), and `install.sh` (§5).
- `snowlinedev/snowline-pm` release: the `snowline-pm` wheel. The repo is
  **private**; assets are fetched with `gh release download`, which rides
  the operator's existing auth. Nothing private ever lands on the public
  platform release.
- Per-service dependency locks: the cutter also attaches a `requirements-<service>.txt`
  exported from the repo's `uv.lock` — specifically `uv export --frozen
  --package <service> --no-emit-workspace`: the bare export emits workspace
  members (the SDK, sibling services) as local *path* entries, unresolvable
  off-checkout — the same "source dragged back in" failure risk #6 catches
  for pm's git pin. Workspace-internal deps are exactly what
  `--no-emit-workspace` drops; they install as wheel pins from the release
  assets alongside the service package itself. The result reproduces the
  tested transitive closure, not whatever PyPI resolves on install day.

On the target, `snowline stack sync` (§5) creates **one venv per service**
with uv (`uv venv --python 3.12` against a uv-managed interpreter, then
`uv pip install --find-links <downloaded-assets> -r requirements-<service>.txt
<package>==<manifest-pinned version>` — per-component, since a PATCH respin
(§4) leaves non-respun components on the prior version). launchd runs
`<venv>/bin/uvicorn` directly —
uv is an install-time tool only, never a runtime dependency, and the plists
need no `WorkingDirectory` because nothing runs from a checkout.

**Why this beats the alternatives weighed in #201:**

- *Homebrew tap*: Python formulas want every transitive dep restated as a
  resource stanza and rebuild from sdists at install time; that's a
  per-release maintenance tax, and a tap has no story for the private pm
  repo. `brew services` is the one thing we'd want from it, and launchd
  templates (§5) cover that directly.
- *Signed tarball with bundled venvs*: relocatable venvs are fragile
  (interpreter paths, dylib references), and signing/notarizing hundreds of
  `.so`s buys nothing here — **Gatekeeper only inspects quarantined files,
  and none of `gh`, `uv`, or `curl` set the quarantine xattr.** No
  double-clickable artifact exists in this design, so signing is pure
  friction. Revisit trigger: the day we ship a `.pkg`/DMG/app bundle, this
  decision reopens.
- *Wheels* are what the codebase already is (pure-Python packages, hatchling
  builds, package-internal alembic migrations — verified: the platform's
  boot-migrate resolves `Path(__file__).parent / "migrations"`, checkout-free;
  binary deps like `psycopg[binary]` arrive as upstream wheels).

### 2.1 Precondition owned by the release-pipeline item

`uv build --all-packages` + boot-migrate-from-wheel must be verified for
**every** service (platform's is verified; governance/memory/pm follow the
same package-internal pattern but each needs the smoke test: install the
wheel into a clean venv, boot against an empty DB, confirm migrate-to-head).

### 2.2 Dashboard: built by the cutter, shipped as a dist tarball

Node stays on the cutting host (§4), never on the target. The release
carries `dashboard-dist-<v>.tar.gz` (the `npm ci && npm run build` output,
contrast-validator and `tsc -b` gates included);
sync unpacks it under the stack root and sets `SNOWLINE_DASHBOARD_DIST`
in the platform env file. **Sync must always set this env** — the code's
fallback path (`parents[2]/dashboard/dist`) points into site-packages
nonsense on a wheel install and must never be relied on.

## 3. Decision D2 — Postgres: Homebrew `postgresql@16`, boot-migrate unchanged

- **Homebrew `postgresql@16`**, managed by `brew services`. Same major
  version as the primary — seeding is a pg_dump/restore pipeline (§7 of
  replication-continuity), and holding the major equal keeps that path
  boring. (Since item 0ebe6a70 the dump is produced by the primary's own
  service and fetched over HTTP rather than by a remote `pg_dump`, but it is
  still a custom-format archive fed to `pg_restore`, so the equal-majors rule
  stands unchanged.) When the primary upgrades majors, the spoke follows in
  the same train.
- *Postgres.app* rejected (GUI app lifecycle, not scriptable as a service
  target); *bundling* rejected (enormous artifact, security-update burden,
  and Homebrew is already a stated prerequisite).
- **Who runs alembic: the services themselves, on boot** — the existing
  lifespan posture (`schema-pr-deploy-needs-live-db-migration`). The
  installer's whole DB job is `createdb` for the four databases
  (`snowline_platform`, `snowline_governance`, `snowline_memory`,
  `snowline_pm`) when absent. It never invokes alembic.

## 4. Decision D3 — one release train, defined by a manifest in the platform repo

**One version spans the stack.** Per-service versioning would create a
compatibility matrix nobody tests — the SDK contract drift guards and pm's
git-pinned SDK dependency already couple the repos in practice; the train
makes that coupling honest.

- **Who cuts a train: the operator's machine, not CI.** `snowline release cut`
  is a **host-side** command. Item #202 forbids GitHub Actions in plugin
  repos, and the pm repo is private — an Actions-driven cut would need a PAT
  crossing the public/private boundary, exactly the gymnastics this design
  refuses. The operator's machine already holds every checkout, `gh` auth for
  both repos, node, and Postgres 16, so the cutter runs there and reaches each
  repo through its local checkout. Wheels are still built per repo, from a
  throwaway git worktree at that repo's blessed sha, and attached to *that
  repo's own* release; nothing private ever lands on the public platform
  release. The executor moved; the mechanics below did not.
- **Scheme:** SemVer-shaped `v0.MINOR.PATCH`. MINOR increments per train
  cut; PATCH is a hotfix rebuild of a train (same blessed set, one component
  respun).
- **What "stable" tags off: blessed main SHAs, no release branches.** A
  solo project cuts trains cheaply; a fix rides the next train (or a PATCH
  respin) rather than a maintenance branch.
- **The manifest is the source of truth:** `release/train.json` in the
  platform repo — `{ "version": "v0.4.0", "components": { "<service>":
  { "repo", "tag", "sha", "wheel" } } }`. The per-component `tag` names the
  release each component's assets are fetched from — it is what makes a
  PATCH respin coherent: `v0.4.1` re-tags and rebuilds **only** the respun
  component; every other entry keeps its `v0.4.0` tag, and sync downloads
  each component from its manifest-recorded tag rather than assuming one
  uniform tag exists on every repo. Cutting a MINOR train = update the
  manifest with the blessed SHAs, tag **each component repo** with the train
  tag (the cutter builds and attaches each repo's own wheels to that repo's
  own release — nothing fights the private-repo boundary), and publish the
  platform release carrying the manifest. `snowline release cut --version
  vX.Y.Z [--respin <component>]` is that procedure, with the component set in
  `release/components.json`: it preflights every checkout (clean, on main,
  HEAD pushed), builds, exports locks, runs the §2.1 smoke tests, writes the
  manifest, then tags and publishes — and is safe to re-run after a partial
  failure, skipping tags and releases that already exist rather than
  duplicating them.
- **Milestone gate (#242):** before building, `cut` maps the train version to
  its release milestone (`vX.Y.Z` -> `snowlinedev/vX.Y`; a PATCH respin
  belongs to its MINOR's milestone) and asks pm's `milestone_status` (MCP
  `pm__milestone_status` through the gateway at `$SNOWLINE_PLATFORM_URL/mcp`).
  It **refuses** when `completion.achievable` is false or
  `completion.required_remaining.count > 0`, listing the first 10 required
  titles + ids, `blocked_by_cancelled`, stale criteria and the
  `readiness_summary`. `--force` prints the same list as a warning and
  proceeds, recording `"gated": {"milestone", "forced": true,
  "required_remaining": N}` in `release/train.json` (additive). **Advisory
  fallback:** if the milestone does not resolve (`registry: null`) or pm is
  unreachable, the cut warns loudly and proceeds, recording
  `"gated": {"skipped": "<reason>"}` — a pm outage never blocks a cut.
  `--dry-run` runs the gate and reports its verdict without cutting.
- The installer resolves "latest" from the platform repo's latest release
  and pins everything else off the manifest inside it.

## 5. Decision D4 — `snowline stack sync`: one idempotent command for install, update, and rollback

Bootstrap, once per machine:

```bash
curl -fsSL https://github.com/snowlinedev/Snowline/releases/latest/download/install.sh | sh
```

`install.sh` (public asset, no auth needed) checks prerequisites (Homebrew,
`gh auth status` — required for the private pm wheel), installs uv if
absent, `uv python install 3.12`, builds the **platform service venv** for
the train it shipped with, points `venvs/platform/current` at it, and
symlinks `~/.local/bin/snowline` → `venvs/platform/current/bin/snowline` —
**through `current`, never at a train-versioned path**, or every later sync
would repoint `current` while PATH stays pinned to (and the keep-2 GC
eventually deletes) the bootstrap venv. It then hands off to:

```bash
snowline stack sync --role spoke [--train vX.Y.Z]
```

There is **no separate CLI environment**: the `snowline` on PATH is the
platform venv's entry point, so sync updates the CLI as a side effect of
updating the platform (a sync run keeps executing the already-loaded old
code; new sync behavior applies from the next invocation). `sync` owns
everything else, idempotently:

1. Resolve the train (`--train` or latest) and download that train's assets
   (`gh release download` per component repo).
2. Build each service's venv at
   `~/Library/Application Support/Snowline/venvs/<service>/<train>/` and
   atomically repoint `<service>/current` — build a temp symlink and
   `rename(2)` it over the old one (`os.replace`; plain `ln -sfn` is
   unlink-then-create and leaves a no-`current` window that a launchd
   KeepAlive respawn or the step-4 kickstart can race). **The symlink swap
   is the whole code deploy and the whole code rollback**; the previous
   train's venv is retained (keep 2).
3. Unpack the dashboard dist; render env files into
   `~/.config/snowline/*.env` from packaged templates — the roam posture
   verbatim (`env.roam.example` promoted from example to template):
   loopback-only binds, full trusted-CIDR list stated explicitly,
   per-process `SNOWLINE_REPLICATION_SOURCE_ID`, `SNOWLINE_PM_ROLE=spoke`.
   Existing operator-edited env files are never clobbered (template
   changes surface as a diff to review).
4. Install/refresh launchd plists from packaged templates
   (`dev.snowline.<service>.plist` pointing at `current/bin/uvicorn`),
   `createdb` missing databases, kickstart exactly the services whose
   `current` changed, health-check the local gateway.

**Update** is the same command with no arguments. **Rollback** is
`snowline stack sync --train vPrev`:

- Code-only rollback (no migration in the delta): symlink swap + kickstart.
  Done.
- Rollback **across a schema migration**: boot-migrate is forward-only by
  design, so the DB does not roll back. On a spoke this is recoverable by
  construction — but by the **re-seed protocol**, not a bare seed: drain the
  outbox (deliver pending spoke-authored events), verify the primary's
  parked set is empty (a park ACKs as delivered, so reseeding over an
  unresolved park loses that write — the runbook's documented data-loss
  case), then `reseed-check` + `seed --reseed` under a fresh epoch, per
  replication-continuity §7 and the runbook's re-seed section. The runbook
  gains a "rollback with schema change" section stating exactly this; it is
  the accepted cost of keeping migrations forward-only.

Tailnet exposure of the local gateway/dashboard is item 39c092c9
(Tailscale Serve), layered on the loopback-only posture above — this spec
just pins that sync never binds anything beyond loopback.

## 6. Roles and config

`--role spoke` is the only role sync accepts in v1 (the primary is
source-run, §1). Role selection drives: `SNOWLINE_INSTANCE_ID`,
`SNOWLINE_PM_ROLE=spoke`, replication interval, and which plist set is
installed. Machine-specific values (tailnet peer address, instance name)
are prompted once and persisted in `~/.config/snowline/stack.json`; sync
re-reads it on every run — the "one command" property survives re-runs.

Pairing + seeding (the actual spoke data bootstrap) stays an explicit,
separate step — item 71317cd6 wires `snowline replicate pair`/`seed`
into a guided `snowline stack bootstrap-spoke` that runs after the first
sync. Software install and data topology are deliberately not fused: sync
must stay safe to run at any time, and seeding is an operator-attended
operation ordered by replication-continuity §7.

`bootstrap-spoke` prompts for topology only — the primary's gateway URL and
this machine's own tailnet address, both persisted into `stack.json` — and
for **no credential**. It asked for the primary's Postgres user until item
0ebe6a70; the primary now serves its own snapshot over its replication-admin
surface, authorized by the stream secret the seed mints while priming, so
`seed.json` carries no primary-side Postgres URL and no password anywhere
(governance decision 1a83031c keeps the hub's Postgres loopback-only).

## 7. Decision D5 — v1 component set and each component's data story

| Component | In v1? | Data story on the spoke |
|---|---|---|
| platform | yes | Scope stream replication (replication-continuity §8) — pair + seed per runbook. |
| governance | yes | Replication shipped (#79) — spoke peer, seeded. |
| memory | yes | Replication shipped (#80) — spoke peer, seeded. |
| pm | yes | Replication-ready (pm PRs #33–36); `SNOWLINE_PM_ROLE=spoke`. |
| dashboard | yes | Stateless static bundle inside the platform release (§2.2). |
| walkthrough-mcp | **no** | Needs no packaging or replication: it already runs natively on the MBP (single-home, cross-registered, continuity §4.1). Registering it against the *local* gateway too is the existing multi-target-heartbeat thread, not this milestone. |
| musher | **no** | Not yet a deployed service; standalone by design, no replicated state. Revisit at its work-item-watcher phase — likely ships as a fifth wheel on the same train with local-empty data. |
| remote-front | **no** | Fly.io deployment surface; unrelated to local install. |

## 8. Hand-off to the implementation items

- **a0ef1bd4 (release pipeline):** the host-side cutter `snowline release
  cut` (`uv build --all-packages`, lock exports, dashboard dist build,
  per-repo tags and releases), the component set in
  `release/components.json`, `release/train.json`, and the §2.1 wheel-boot
  smoke tests. First output: train `v0.1.0`.
- **b70b0359 (one-command install/update):** `install.sh` + `snowline stack
  sync` per §5–6, env/plist templates promoted from `ops/roam/`. Includes
  the runbook update this spec obsoletes in place: the spoke is now **four**
  services and **four** databases (pm joins the drill — the runbook's
  prerequisite list and service set predate pm on the spoke).
- **71317cd6 (spoke bootstrap, pm scope):** `snowline stack bootstrap-spoke`
  wrapping pair/seed + `SNOWLINE_PM_ROLE=spoke` verification.
- **39c092c9 (Tailscale Serve):** hosted interface on top of the loopback
  binds.
- **924fc510 (first MBP install, human):** the acceptance run: fresh-ish Mac
  with brew + gh → curl bootstrap → sync → bootstrap-spoke → working local
  gateway with all four services + dashboard; then one train update and one
  code-only rollback exercised for real.

## 9. Risk register

1. **Wheel-boot parity** per service (§2.1) — gate on the smoke test, not
   assumption.
2. **`SNOWLINE_DASHBOARD_DIST` must always be set by sync** (§2.2 fallback
   trap).
3. **uv-managed interpreter lifecycle:** venvs pin the uv-installed CPython;
   an interpreter GC/upgrade must not orphan `current` venvs — sync verifies
   the interpreter exists before swapping symlinks, reinstalling it if
   needed.
4. **gh auth expiry** breaks pm asset fetch mid-sync — sync checks
   `gh auth status` up front and fails before touching any symlink.
5. **Env template drift** vs operator-edited env files — sync diffs and
   reports, never overwrites (§5 step 3).
6. **pm's SDK dependency is a git pin today.** A naive lock export would
   put `snowline-plugin-sdk @ git+…` in pm's requirements — dragging a
   source clone back into the "not-from-source" install. The release
   pipeline must make pm's environment resolve the SDK to the **train's
   SDK wheel** (rewrite the pin at export time, or move pm to a versioned
   SDK dependency once wheels exist). Acceptance: a pm venv built from
   release assets alone, offline from git.
