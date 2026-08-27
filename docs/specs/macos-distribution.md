# macOS distribution — the packaged stable channel (installable-v1)

> **Status: draft.** Design for milestone `snowlinedev/installable-v1`
> (issue #201): the MacBook Pro instance is installed and updated from
> **packaged stable builds**, never a source checkout. This spec makes the
> five calls the implementation items build against: artifact format,
> Postgres strategy, version scheme, update/rollback path, and the v1
> component set with each component's data story. Companion specs:
> `replication-continuity.md` (§5 spoke posture, §7 seeding),
> `docs/ops/roam-runbook.md` (the operational drill this channel automates),
> `deploy-continuity.md` (restart visibility), `ui-shell.md` §6 (dashboard
> dist resolution).

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
full replication streams (`replication_stream.py` + apply paths are live
code), and the roam runbook already stands both up as spoke services. The
open data-story questions narrow to walkthrough and musher (§7).

## 2. Decision D1 — artifact format: wheels on per-repo GitHub Releases, installed by uv into managed venvs

Each release ships **Python wheels** built in CI and attached to a GitHub
Release on the repo that owns the code:

- `snowlinedev/Snowline` release: wheels for `snowline-platform`,
  `snowline-governance`, `snowline-memory`, `snowline-plugin-sdk` (one
  `uv build --all-packages` over the workspace), plus three non-wheel
  assets: `dashboard-dist-<v>.tar.gz` (§2.2), `train.json` (§4), and
  `install.sh` (§5).
- `snowlinedev/snowline-pm` release: the `snowline-pm` wheel. The repo is
  **private**; assets are fetched with `gh release download`, which rides
  the operator's existing auth. Nothing private ever lands on the public
  platform release.
- Per-service dependency locks: CI also attaches a `requirements-<service>.txt`
  exported from the repo's `uv.lock` (`uv export --frozen`), so the installed
  environment reproduces the tested transitive closure, not whatever PyPI
  resolves on install day.

On the target, `snowline stack sync` (§5) creates **one venv per service**
with uv (`uv venv --python 3.12` against a uv-managed interpreter, then
`uv pip install --find-links <downloaded-assets> -r requirements-<service>.txt
<package>==<train-version>`). launchd runs `<venv>/bin/uvicorn` directly —
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

### 2.2 Dashboard: built in CI, shipped as a dist tarball

Node stays in CI. The release carries `dashboard-dist-<v>.tar.gz` (the
`npm run build` output, contrast-validator and `tsc -b` gates included);
sync unpacks it under the stack root and sets `SNOWLINE_DASHBOARD_DIST`
in the platform env file. **Sync must always set this env** — the code's
fallback path (`parents[2]/dashboard/dist`) points into site-packages
nonsense on a wheel install and must never be relied on.

## 3. Decision D2 — Postgres: Homebrew `postgresql@16`, boot-migrate unchanged

- **Homebrew `postgresql@16`**, managed by `brew services`. Same major
  version as the primary — seeding is a pg_dump/restore pipeline (§7 of
  replication-continuity), and holding the major equal keeps that path
  boring. When the primary upgrades majors, the spoke follows in the same
  train.
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

- **Scheme:** SemVer-shaped `v0.MINOR.PATCH`. MINOR increments per train
  cut; PATCH is a hotfix rebuild of a train (same blessed set, one component
  respun).
- **What "stable" tags off: blessed main SHAs, no release branches.** A
  solo project cuts trains cheaply; a fix rides the next train (or a PATCH
  respin) rather than a maintenance branch.
- **The manifest is the source of truth:** `release/train.json` in the
  platform repo — `{ "version": "v0.4.0", "components": { "<service>":
  { "repo", "sha", "wheel" } } }`. Cutting a train = update the manifest
  with the blessed SHAs, tag **each component repo** with the train tag
  (each repo's release workflow builds and attaches its own wheels — no
  cross-repo checkout, so no PAT gymnastics and nothing fights the
  private-repo boundary), and publish the platform release carrying the
  manifest. A `snowline release cut` helper can automate the tagging later;
  v1 is a documented procedure.
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
the train it shipped with, symlinks its `snowline` entry point into
`~/.local/bin`, and hands off to:

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
   atomically repoint `<service>/current`. **The symlink swap is the whole
   deploy and the whole rollback**; the previous train's venv is retained
   (keep 2).
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
  construction: drain the outbox (deliver pending spoke-authored events),
  then **reseed from the primary** (`snowline replicate seed`, the §7
  ordering). The runbook gains a "rollback with schema change" section
  stating exactly this; it is the accepted cost of keeping migrations
  forward-only.

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
must stay safe to run at any time, and seeding is a §7-ordered, operator-
attended operation.

## 7. Decision D5 — v1 component set and each component's data story

| Component | In v1? | Data story on the spoke |
|---|---|---|
| platform | yes | Scope stream replication (spec §8) — pair + seed per runbook. |
| governance | yes | Replication shipped (#79) — spoke peer, seeded. |
| memory | yes | Replication shipped (#80) — spoke peer, seeded. |
| pm | yes | Replication-ready (pm PRs #33–36); `SNOWLINE_PM_ROLE=spoke`. |
| dashboard | yes | Stateless static bundle inside the platform release (§2.2). |
| walkthrough-mcp | **no** | Needs no packaging or replication: it already runs natively on the MBP (single-home, cross-registered, continuity §4.1). Registering it against the *local* gateway too is the existing multi-target-heartbeat thread, not this milestone. |
| musher | **no** | Not yet a deployed service; standalone by design, no replicated state. Revisit at its work-item-watcher phase — likely ships as a fifth wheel on the same train with local-empty data. |
| remote-front | **no** | Fly.io deployment surface; unrelated to local install. |

## 8. Hand-off to the implementation items

- **a0ef1bd4 (release pipeline):** per-repo release workflows (`uv build
  --all-packages`, lock exports, dashboard dist build), `release/train.json`
  + cut procedure, and the §2.1 wheel-boot smoke tests. First output: train
  `v0.1.0`.
- **b70b0359 (one-command install/update):** `install.sh` + `snowline stack
  sync` per §5–6, env/plist templates promoted from `ops/roam/`.
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
