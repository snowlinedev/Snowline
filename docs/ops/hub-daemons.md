# Hub services as system LaunchDaemons

Work item 239ec238 (issue #239); owner decision 66a29a6c, option (a).

## Why

On 2026-10-01 a power loss rebooted the hub Mac. Postgres (a Homebrew
service) came back by itself, but the four Snowline services were per-user
LaunchAgents in `~/Library/LaunchAgents`, which load only in a logged-in GUI
session. They stayed down about 2.5 hours until someone logged in; every agent
session lost the Snowline MCP surface and spokes could not replicate.

## What the install does

`sudo ops/hub/install-daemons.sh` (idempotent; self-elevates with sudo), for
each of platform / pm / governance / memory:

1. `launchctl bootout gui/501/dev.snowline.<svc>` (errors ignored).
2. Moves `~/Library/LaunchAgents/dev.snowline.<svc>.plist` to
   `...plist.disabled-<YYYYMMDD>` so the agent cannot reload at login and
   double-bind the port.
3. Copies `ops/hub/launchdaemons/dev.snowline.<svc>.plist` to
   `/Library/LaunchDaemons` (root:wheel, 644) — before step 1, so the
   replacement is on disk before anything is stopped — and
   `launchctl bootstrap system` (RunAtLoad + KeepAlive: that starts it; no
   kickstart, which would only kill it mid-startup).
4. Installs the autocut launchd timer from `ops/hub/launchdaemons/` the
   same way, but only bootstraps it (never kickstarts) — and only when the
   in-process autocut host is NOT enabled (`~/.config/snowline/
   autocut.enabled` absent); with the host enabled (the hub's default) the
   timer is not installed, and a loaded timer next to an enabled host is
   reported as a fault. Left alone when unchanged and loaded, skipped
   (non-zero exit) if a cut is running.
5. Polls the four `/health` URLs for up to 60s and prints UP/DOWN.
6. Runs `ops/hub/install-backup-agent.sh` as the owner (after the services
   are up, so its proving run cannot land in a boot migration's window).
   That script is the backup agent's own installer and can be run on its
   own, without sudo, whenever the backup needs (re)proving — re-running
   `install-daemons.sh` restarts all four services and is NOT the way to
   do that. Its outcome is printed, not folded into `install-daemons.sh`'s
   exit status, which is the deploy's own result (services and timer); it
   is called with `--deploy`, so a dump in progress is skipped rather than
   waited on and the proof is bounded to ten minutes. It (re)loads `ops/hub/launchagents/dev.snowline.backup.plist`
   into `gui/501` only when the tracked plist differs from the one last
   confirmed loaded or the unit is not loaded (waiting first for a dump in
   progress); a unit that is loaded and current but whose last run exited
   with anything other than 0 or 3, or for which there is no evidence it
   itself dumped in the last two hours (a fresh dump plus the unit's own
   `run exit` log line — a manual dump alone does not count), is not
   reloaded but proven again — except exit 78 (launchd could not spawn the
   program), which IS reloaded, since a reload is what clears launchd's
   penalty box. The plist
   carries the canonical checkout path for the script on purpose (a path
   taken from whichever checkout runs an install could be a scratch
   worktree); the installer refuses to run from any other checkout and
   refuses to load a unit whose script is missing — update the path in
   the tracked plist if the hub checkout ever moves (nothing else holds
   it).
   Operator overrides live in `~/.config/snowline/backup.env`, never in the
   plist. It stays a per-user agent on purpose ("DB backups" below).
7. If step 6 (re)loaded the backup agent, the installer proves it by firing one run
   through launchd (`kickstart`, so it runs in the agent's own session) and
   fails `install-backup-agent.sh` (reported by `install-daemons.sh`, whose
   own exit status it does not change) unless that run exited 0 AND a dump
   newer than it was confirmed — in the destination, or, when the install's own context
   cannot read it (ssh without Full Disk Access), in the agent's log lines
   for the run. An
   exit 3 (a manual run held the lock; launchd runs one instance per label,
   so the hourly fire itself never can) is waited out and
   judged by the same evidence. An unchanged, loaded agent is left alone
   only when its last run was fine (exit 0, or 3) AND a dump landed in the
   last two hours (by the same two sources).

The daemons run as `UserName=seanlynch`, `GroupName=staff`, so the mode-600
env files in `~/.config/snowline/` stay readable. Logs are unchanged
(`~/Library/Logs/snowline-<svc>.log`). `KeepAlive` is plain `true` with
`ThrottleInterval` 10 so a crash restarts without spinning.

Each plist runs `ops/hub/launchdaemons/run-service.sh <svc>`, which sources the
env file, then:

- waits up to ~120s for `pg_isready` (host parsed from the service's
  `SNOWLINE_<SVC>_DATABASE_URL`; local socket if none) because Postgres races
  the services at boot;
- platform only: waits up to ~180s for the tailnet bind address
  `100.81.176.75` to appear in `ifconfig` (uvicorn binds before any app code
  runs, so this cannot be a Python-side retry);
- logs each wait to the service log, then `exec`s uvicorn. After a bounded wait
  it execs anyway and launchd's KeepAlive retries.

The wrapper is run from the platform checkout
(`/Users/seanlynch/Projects/snowline-platform/ops/hub/launchdaemons/`), so the
checkout must be on a branch that contains it (i.e. merge this change and
update the checkout before installing).

Governance/memory may log a registration failure against the tailnet IP before
Tailscale is routable and recover on retry; that is expected.

## Reboot drill (acceptance)

```
sudo shutdown -r now
```

Do NOT log in. From another machine on the tailnet, within a few minutes:

```
curl -fsS http://100.81.176.75:8850/health
```

and on the hub (over ssh) `curl -fsS http://127.0.0.1:{8801,8802,8803}/health`.
All four must answer. If one does not, read
`~/Library/Logs/snowline-<svc>.log`.

## Rollback

`sudo ops/hub/uninstall-daemons.sh` restores each service to its per-user
agent: it checks first that there is an agent plist to go back to (a live
`~/Library/LaunchAgents/dev.snowline.<svc>.plist`, else the newest
`.disabled-*` copy), then boots the system daemon out, removes its plist
from `/Library/LaunchDaemons`, puts the agent plist back and bootstraps it
into `gui/501` (kickstarting it when it has no `RunAtLoad`). A service with
NO agent plist to restore is left running as a daemon, with a message and a
non-zero exit: recreate the agent plist by hand, then re-run. The backup
agent is not part of the move and is left as it is. Neither script boots the autocut timer out
mid-cut: a running cut is refused up front (exit 75, or waited for with
`--wait`) and, if one starts during the install, the autocut unit is skipped
with a message and a non-zero exit. The backup agent is only reloaded after
a dump in progress (it holds `backup.lock`) has finished; a fire landing in
the instant between the last probe and the bootout would lose that hour's
dump (the proving run that follows replaces it).

## Deploy routine change

Restarting a service is now
`sudo launchctl kickstart -k system/dev.snowline.<svc>` instead of
`launchctl kickstart -k gui/$UID/dev.snowline.<svc>`.

No sudo is needed for the routine deploy restart, though. The daemon process
runs as `seanlynch` with `KeepAlive true`, so killing it is enough and launchd
relaunches it (verified 2026-10-15: pm back on a new pid with `/health` 200
within ~2s):

```sh
kill -TERM "$(launchctl print system/dev.snowline.<svc> | awk '/^\tpid = /{print $3}')"
```

This is the restart an agent session uses after `git pull --ff-only`; the
`sudo launchctl kickstart -k` form is equivalent and also covers a daemon
whose process is not currently running.

**Before restarting the platform, check for a cut in flight:**
`.venv/bin/snowline release autocut --status`. If it prints `lock: HELD`, an
auto-cut is running (about 6 minutes), and the pid shown is the cut's child
process, not uvicorn. A `kill -TERM` then makes the platform wait for the
cut, up to 10 minutes, then kill it and exit (see "Auto-cut" below).
A `launchctl kickstart -k` or `bootout` does not wait that long: launchd
SIGKILLs the job after its exit timeout (20 seconds by default). The cut runs
in its own process group, so that kill is not aimed at it — but its output
pipe leads to the dead platform, so the orphan dies with a broken pipe at its
next log line, half-done, and releases the lock. Do not expect it to finish:
the train is left half-published until the next tick recovers it. Wait for
`lock: free` instead.

## Stack-sync guard

`check_not_source_hub` (`src/snowline_platform/stack/sync.py`) refuses to
install the packaged stack when unmanaged `dev.snowline.*` plists exist and
there is no `stack.json`. It checks both `~/Library/LaunchAgents` and
`/Library/LaunchDaemons`.

## Auto-cut (hourly patch trains, #258)

The MBP spoke is a warm standby. It is only worth switching to if it carries
the code the hub has been running, so the hub cuts a train on its own whenever
main moves, and the spoke's `stack sync --auto` timer picks it up.

**How it runs: inside the platform daemon (the default).** The hub's
platform process hosts the auto-cut as a background task
(`src/snowline_platform/release/autocut_host.py`, item a25fa809). There is
nothing to install and no launchd job to register. Decision 9ce14c69 makes
the hub a seat that agents deploy continuously, and registering a new launchd
job is the one deploy step an agent session cannot take. So enabling the
auto-cut is a pull, a restart, and one marker file:

- **Enable:** `touch ~/.config/snowline/autocut.enabled`. The host re-reads
  the marker on every tick, so a running platform picks it up at the next
  tick. Restart the platform to start counting from now.
- **Disable:** `rm ~/.config/snowline/autocut.enabled`, or
  `touch ~/.config/snowline/autocut.disabled`. The kill switch wins over the
  marker, and the launchd timer below honours it too.
- **Cadence:** the first tick is 3 minutes after the platform starts, then
  every hour after that.
- **Log:** one line per tick in `~/Library/Logs/snowline-platform.log`
  (`autocut host tick: noop`, `ok`, `gated`, `failed`, `locked`, ...), plus
  the cut's own output prefixed `autocut:`. A platform without the marker
  logs `autocut host idle: no enable marker (...)` once at startup and never
  cuts.

The host refuses to cut, whatever the switches say, when:

- `~/.config/snowline/stack.json` says `role=spoke` (a packaged spoke never
  cuts trains);
- the `SNOWLINE_PLATFORM_URL` the gates would use is not this platform's own
  address and port, or its host is not an address of this machine (the
  gates must judge the hub that cuts);
- the platform is not running from a git checkout (the hub runs from source,
  and a wheel has no checkouts to cut from).

A refusal is logged as a WARNING once each time the reason changes, not on
every tick.

The cut runs as a **child process**, the platform venv's own
`snowline release autocut`, in its own process group. Its output goes to the
platform log prefixed `autocut:`, and `/health` and the gateway keep serving
during the ~6 minutes. The child and its subprocesses get a **clean**
environment, built
like the launchd timer's: `HOME`, `USER` and locale, `SNOWLINE_PLATFORM_URL`,
and a `PATH` that starts with `~/.local/bin:/opt/homebrew/bin` (uv, gh, npm)
and ends with the platform venv's `bin`. `~/.config/snowline/autocut.env` is
layered on top. The host parses it as `KEY=VALUE` lines and never executes
it. The daemon's own environment is **not** passed on. It holds the live
`SNOWLINE_*_DATABASE_URL`s, and the cut's test runs must never see those.

**Shutdown during a cut.** Stopping the platform cancels the host's sleep at
once. A cut in flight is different. The platform waits up to 10 minutes for
it to finish, so it is not killed half-published (tags pushed, releases
missing). After that it kills the cut's whole process group: SIGTERM, then
SIGKILL 10 seconds later. The platform then exits, and launchd relaunches it.
The kernel releases the lock when the child dies. The cutter is safe to
re-run: the next tick lands any uncommitted record and skips tags and
releases that already exist. A platform restart during a cut can therefore
take up to about 10 minutes, and that includes the deploy chain's
`kill -TERM`. Check `snowline release autocut --status` first (see "Deploy
routine change" above). launchd's own stop (`kickstart -k`, `bootout`)
SIGKILLs after about 20 seconds and does not get the full wait.

`GET /standby` (`hub.autocut`) shows the live host state next to the last
report: `running` (a cut is running right now), `next_tick_at`, and
`host_enabled`.

**`snowline release autocut --status`** prints the switches, whether the
run lock is held (a cut is running, with the holder's pid: the cut's child
process for the in-process host), and the latest
report's outcome, time and last cut. It is read-only, and it needs no release
config.

**Alternative: the launchd timer.** `dev.snowline.autocut` (below) is still
supported, for a hub whose platform does **not** run from source. Both may be
installed at once. The run lock serialises them: whichever fires second
records `locked` and exits. The in-process host is the hub's default and
needs no launchd registration.

**The launchd timer.** `dev.snowline.autocut` is a system LaunchDaemon (runs as
`seanlynch`, no login needed) with `StartInterval` 3600 and `RunAtLoad`
false. It runs `ops/hub/launchdaemons/run-autocut.sh`, which execs
`.venv/bin/snowline release autocut` from the platform checkout. Not
`uv run`: that would re-sync the venv the running services import.

**One run** (either way):

1. Takes a lock (`~/Library/Application Support/Snowline/autocut.lock`). If a
   cut is already running, this fire does nothing and exits 0.
2. Fetches each train component's checkout (platform, pm; any component
   later added to `release/components.json`). Then it compares `origin/main`
   with the sha recorded in `release/train.json`. Commits that touch only
   `release/train.json` don't count, so autocut's own record commit doesn't
   trigger the next cut. If nothing moved, it writes a one-line report and
   exits 0.
3. Gates. Any failure here exits 2 and nothing is cut. Every gate requires:
   - every checkout is on `main`, clean, and at `origin/main`. So the hub must
     already run main: after a merge, deploy (pull + restart) first.
   - `gh auth status` is OK.
   - `SNOWLINE_PLATFORM_URL/health` answers 200.
   - every train service (platform, pm, governance, memory) is registered on
     `/plugins` and `up`. One exception: `degraded` is accepted when **every**
     reason in its `degraded_reason` (joined with `; `) is
     `peer X not delivering for …`. That reason means the standby is
     powered off, and that is exactly the state this timer fixes. A
     `delivery tick raised N consecutive times` reason means the replication
     loop on the hub is crash-looping, and it refuses the cut. So does any
     reason it doesn't recognise, or `degraded` with no reason. Pass
     `--strict-health` to require `up` with no exceptions.
   - the train's milestone gate answers ok. Autocut refuses if the gate
     *skipped* (could not judge), where `release cut` would only warn.
4. Version: the next **patch** of the recorded train (v0.5.1 to v0.5.2). The
   version pattern only covers `v0.x.y`. A future `v1.x` train needs the
   pattern extended (`_PATCH_RE` in `release/autocut.py`, mirroring
   `release/model.py`); until then autocut fails loudly instead of guessing. It
   never bumps the minor. The current minor's milestone is achieved and the
   next one is open, so a patch is the only train a timer may cut. If a tag is
   already taken at a different sha, autocut skips that version.
5. Runs the ordinary `cut()` in-process, about 6 minutes: tests, builds,
   smoke, tag, publish.
6. Only after the cut succeeds, it commits `release/train.json` (only that
   path) on main as `release: train vX.Y.Z (autocut)` and pushes.
   `--no-commit-manifest` skips this step. If the push is rejected (main
   moved during the cut), it undoes the commit, leaves the file uncommitted,
   and reports `published_unrecorded` (exit 1). The train is live on GitHub
   at that point; only the record on main is missing. The next run lands it as
   `... (autocut: record recovered)`, and does the same for a record left by
   a manual `release cut` run without `--commit-manifest`.

A failed cut never leaves a committed record. Autocut never prompts and never
passes `--force`.

**Where to look.**

- Report: `~/Library/Application Support/Snowline/autocut-report.json`, next to
  the spoke's `sync-report.json` and `bootstrap-report.json`. It holds the
  latest run's `outcome` (`noop` / `ok` / `gated` / `failed` /
  `published_unrecorded`), `detail`,
  per-component `recorded_sha` / `origin_sha` / `moved`, the `gates`, and
  `last_cut` (`version`, `shas`, `at`, `outcome`). `last_cut` carries over
  across noop and gated runs, so it always answers "last cut". A run that
  only landed a leftover record sets `last_cut` to that train, with outcome
  `recorded`.
- History: `autocut-history/` gets an entry for each run that is not a noop
  and whose outcome or detail differs from the previous run. A hub that stays
  gated ("behind origin/main") until someone deploys therefore leaves one
  entry, not one per hour.
- Readiness: the hub platform reads this report from disk to fill the
  `hub.autocut` and `hub.latest_cut` fields of `GET /standby` / `health.standby`
  and the dashboard's Standby panel (#259). `latest_cut` is the newest of
  `release/train.json`, a published `last_cut`, and the running wheel's own
  train version (when the platform runs from a wheel rather than source). It
  is what the spoke's running train gets compared against. Any peer can also read the report,
  redacted, at `GET /stack/report`. If the platform runs as a different
  account from the autocut, point `SNOWLINE_STANDBY_HOME` at the home that
  owns the report.
- Log: the platform's log for the in-process host;
  `~/Library/Logs/snowline-autocut.log` for the launchd timer.
- Exit codes: 0 noop/ok/locked, 2 gated, 1 failed or published_unrecorded.

**Environment.** The in-process host passes `SNOWLINE_PLATFORM_URL` (from
`autocut.env` when set there, else the platform's own); the plist sets it for
the timer. Autocut uses it for
the health gate and for the milestone gate, which skips without it, and
autocut refuses a skipped gate. `--platform-url` overrides it. Both read
`~/.config/snowline/autocut.env` if the file exists (the timer's wrapper
sources it; the host parses it into the cut's environment). The
cutter runs each component's tests in its throwaway build tree (its own
`.venv-test` beside the build tree, never the live checkout's `.venv`) against scratch databases it
provisions and drops (`snowline_cut_test_<name>`). Set
`SNOWLINE_PLATFORM_TEST_DATABASE_URL`, `SNOWLINE_GOVERNANCE_TEST_DATABASE_URL`,
`SNOWLINE_MEMORY_TEST_DATABASE_URL` or `SNOWLINE_PM_TEST_DATABASE_URL` there
only to point at a different Postgres; an operator-set URL is used as is and
no scratch database is created for it.

**Try it by hand:**
`SNOWLINE_PLATFORM_URL=http://100.81.176.75:8850 .venv/bin/snowline release autocut --dry-run`.
A dry run decides, gates, and plans. It builds, commits, and reports nothing.
A dry run also holds the lock. If the hourly fire lands while a manual
`--dry-run` is running, that fire exits silently as `locked` and the next
chance to cut is an hour later.

**Disable.**

- Without sudo: `touch ~/.config/snowline/autocut.disabled`. The in-process
  host then skips every tick and the timer's wrapper exits 0 on every fire.
  `rm` the file to re-enable.
- Fully: `sudo launchctl bootout system/dev.snowline.autocut`, or
  `sudo ops/hub/uninstall-daemons.sh`, which also removes the services'
  daemons. A bootout SIGTERMs a cut that is in flight. Check
  `~/Library/Logs/snowline-autocut.log` first, or use the scripts, which
  check for you (below).

**Credentials.** The cut pushes tags, creates releases, and pushes the record
commit. On the hub, `gh`'s token lives in `~/.config/gh/hosts.yml` (mode 600,
not the keychain), and git's github.com credential helper is
`gh auth git-credential`. A daemon running as `seanlynch` with `HOME` set can
reach both without a login session. If gh is ever re-authenticated into the
keychain, the `gh_auth` gate refuses every run (exit 2, in the report). The
fix is `gh auth login --insecure-storage`, or moving the plist to a gui/501
LaunchAgent. Check the helper in each checkout
(`git -C <checkout> config credential.helper`). If it is `osxkeychain`, a
daemon cut can fail at its first `git push origin <tag>`. That is the first
publishing step, so nothing is published before it. The fix is
`git config credential.helper '!gh auth git-credential'` in both checkouts,
so pushes use `hosts.yml` the way `gh` does.

**Install (launchd timer only; the in-process host needs none).**
`sudo ops/hub/install-daemons.sh` (idempotent) copies the plist
to `/Library/LaunchDaemons` and runs `launchctl bootstrap` on it. It does
**not** kickstart it, because a kickstart would start a cut immediately. The
first run happens up to an hour later. To cut now, run
`sudo launchctl kickstart system/dev.snowline.autocut`.

Re-running install (the deploy path) boots the timer out first, and so does
uninstall. A bootout would kill a cut that is in flight, so both scripts
first test the flock on `~/Library/Application Support/Snowline/autocut.lock`.
While a cut holds it they refuse with exit 75, naming the lock and the log.
`--wait` polls every 15s for up to 15 minutes instead. A fire that starts in
the few seconds between the check and the bootout is not covered.

## DB backups (hourly, `dev.snowline.backup`)

Decision c68ec655 (supersedes e4578136): every live Postgres store on the hub
is dumped every hour into iCloud Drive. The unit is the owner's **per-user
LaunchAgent**, `~/Library/LaunchAgents/dev.snowline.backup.plist`, tracked
at `ops/hub/launchagents/dev.snowline.backup.plist` and installed by
`install-daemons.sh` (step 6 above) — the #220 root cause was exactly that
this unit's files lived outside main.

**Why it is not a system daemon like the services.** A dump needs the
owner's session twice over: Postgres on the hub is the owner's per-user
agent (`homebrew.mxcl.postgresql@16`), and iCloud Drive syncs per session.
After a FileVault boot that session is always there, and without it a
daemon could not dump either. Moving the timer to the system domain would
therefore add no coverage, while a system-domain process writing into
`~/Library/Mobile Documents` is a TCC question with no session to answer the
prompt. So the agent stays (item f304ec06 asked for the daemon; this is the
reasoned answer).

**What runs.** The plist fires `ops/hub/snowline-backup.sh` every 3600 s (no
RunAtLoad; the first timed run is an hour after loading). The script:

- takes a flock on `~/Library/Application Support/Snowline/backup.lock` for
  the whole run (a second run steps aside with exit 3);
- waits up to 120 s for `pg_isready` (Postgres is the owner's per-user
  LaunchAgent, so at boot it can start after this agent fires), then dumps
  every store (today `snowline_platform`, `snowline_pm`,
  `snowline_governance`, `snowline_memory`, `snowline_musher`,
  `snowline_marketing`) with `pg_dump -Fc --lock-wait-timeout=60000` (a
  dump never blocks a service's migration for more than a minute; that
  store is then a failure for the hour), verifies each archive with
  `pg_restore -l`, then renames `.partial` to the final name — one store
  failing does not skip the others, and the run exits 1 if any did;
- the store list comes from the server's own catalog: every `snowline_*`
  database, except scratch ones; one this role cannot connect to (parked,
  or CONNECT revoked) is a failed store — logged by name, and the run
  ends incomplete (exit 1), never silently whole. A scratch name is one with a `_test`, `_dryrun`, `_e2e`
  or `_restore` component (as a whole component: `snowline_pm_test_sp` and
  `snowline_restore` are scratch, `snowline_testimonials` would be a store)
  and the legacy `snowline_dev`; only plain-identifier names — so a new
  service's store is backed up from its first hour without a code change
  (other projects' databases on the same cluster are not this job's to
  dump). A readable catalog with no
  `snowline_*` store at all is treated as the wrong cluster: the known
  stores are tried and the run fails loudly rather than exiting 0 with
  nothing dumped. If the catalog cannot be read the run falls back to the
  six known stores and says so. Setting `SNOWLINE_BACKUP_DBS` explicitly
  replaces the list (plain identifiers only); live stores it leaves out are
  noted in the log;
- refuses to run if the iCloud Drive root is missing (`mkdir -p` would
  otherwise recreate the folder locally and every run would "succeed" while
  nothing left the machine), and probes a real file create in the
  destination before any dump. This is a presence check only: if iCloud
  Drive is turned off with "keep a copy", the folder stays and the guard
  cannot tell, so confirm from another device now and then that a recent
  dump is actually in iCloud;
- writes owner-only (umask 077, destination 0700, and any 0644 dump found
  in the folder is tightened to 0600 each run): the platform dump carries
  the live replication HMAC secrets, so a dump is as sensitive as the hub;
- prunes each store's dumps older than `SNOWLINE_BACKUP_RETENTION_DAYS`
  (30), always keeping a store's newest three whatever their age (a store
  whose dumps keep failing never loses its last good copy), by the UTC
  stamp in the name (the same rule `--check` uses; an
  iCloud placeholder's mtime is eviction time), including evicted
  `.NAME.icloud` placeholders; the legacy `snowline_dev-*` dumps are never
  pruned (their permissions are tightened like every `snowline_*` dump's). Any argument other than `--print-dest` / `--check` is refused
  (exit 2) so a typo cannot start a real run.

Dumps land in `~/Library/Mobile Documents/com~apple~CloudDocs/Snowline/db-backups/`
as `<store>-<YYYYMMDD-HHMMSS>Z.dump` (UTC). Log: `~/Library/Logs/snowline-backup.log`.
The `.env` secrets are deliberately not backed up. Overrides go in
`~/.config/snowline/backup.env` (sourced by the script as the owner; never
in the plist, which the install replaces with the tracked copy):
`SNOWLINE_BACKUP_DIR` (a non-iCloud path skips the iCloud root check but
must already exist — it is never created, so an unplugged volume cannot be
replaced by a local folder), `SNOWLINE_BACKUP_RETENTION_DAYS`,
`SNOWLINE_BACKUP_DBS` (plain identifiers, space-separated). The file
supplies defaults: a `SNOWLINE_BACKUP_*` variable already exported in the
environment wins over it (a `note:` log line says so), which is how a
scratch test points the script elsewhere; the agent's environment carries
none, so under launchd the file is authoritative.
`bash ops/hub/snowline-backup.sh --print-dest` prints the destination the
script resolves and `--check <minutes>` reports (OK / NONE / UNREADABLE / MISSING, exit 0/1/2)
whether a dump newer than that many minutes exists there, judged by the
UTC stamp in the file name (an iCloud placeholder's mtime is eviction
time); the install runs both as the owner, so root never reads the owner's
iCloud folder. The file is sourced under `set -a`, like the services' env files, so a
plain `PGPORT=5433` line there reaches the pg tools.

**Rolling out this change on the hub.** The hub checkout had an untracked
hand-restored copy of `ops/hub/snowline-backup.sh` from 2026-10-23 (the
#223 script, kept running while the PR was open), hidden from `git status`
by a `.git/info/exclude` entry. Git overwrites an ignored file on checkout
(unlink and rewrite, a sub-second window), so a plain `git pull --ff-only`
is the whole step: the tracked script lands and the agent keeps dumping
from the same path, now with the hardened script; do the pull away from
the minute the agent fires (a StartInterval unit fires at load time plus
multiples of an hour, not on the hour: `tail` the log for the last stamp). Afterwards drop the stale
exclude line. Do NOT delete the copy first: the hourly agent would spawn a
missing program in the meantime (EX_CONFIG, penalty box). The agent plist
already installed on the hub (the #223 one) runs the script directly with
a `WorkingDirectory` and an explicit `SNOWLINE_BACKUP_DIR` equal to the
default; the tracked one runs it via `/bin/bash` with neither. Same
behaviour but for one thing — the old plist has `RunAtLoad`, so it also
dumped at every login; the tracked one does not (the first timed run is an
hour after loading). Otherwise different bytes only: the next `ops/hub/install-backup-agent.sh`
(run as the owner, from the canonical checkout after the pull — from any
other checkout it refuses) will therefore replace and reload it once and
prove it with one run. Once the pull has landed the old plist is already
running the new script, which holds `backup.lock`, so the installer waits
for a dump in progress; only an installer run BEFORE the pull (the
pre-#319 script holds no lock) could boot the agent out mid-dump.

**Is it alive?** Do this whenever you touch the hub — the job has silently
died twice (2026-07-09 to 09-04 after the repo move; 2026-09-13 to 10-23
while the script existed only on an unmerged branch):

```sh
launchctl print gui/501/dev.snowline.backup | grep -E 'last exit code|runs ='
ls -t ~/Library/Mobile\ Documents/com~apple~CloudDocs/Snowline/db-backups | head -6
```

`last exit code = 0` and a dump under an hour old is healthy. Exit 1 with
log lines says which store failed; every run begins with a `run start`
line and ends with a `run exit <code>` line, which the installer reads
when it cannot see the destination. Exit 2
means the run could not start at
all (destination missing or unwritable, iCloud root absent, lib.sh missing,
lock file unopenable) or refused its configuration (a `backup.env` that
does not source, a bad `SNOWLINE_BACKUP_RETENTION_DAYS` or
`SNOWLINE_BACKUP_DBS`, an unknown argument) — the log line names which. Exit 3 means the run stepped aside because another
backup run held the lock (a manual or proving run racing the hourly fire;
harmless once).
A log line "interrupted (SIGTERM/SIGINT/SIGHUP)" (exit 143, or `(never exited)`
in launchctl) means the run was killed mid-dump — a bootout, logout or
shutdown; its partial was removed and the next tick dumps again.
`78: EX_CONFIG` means launchd could not spawn the program (script path
missing — check the checkout).

**Restore.** To inspect first (recommended):

```sh
createdb snowline_restore
pg_restore --no-owner -d snowline_restore <dump>
# ... inspect ...
dropdb snowline_restore
```

(A `*_restore` database is excluded from the hourly dump, so a lingering
inspect database costs nothing but disk; drop it when done anyway.)

To overwrite the live store (destructive; stop that service first):

```sh
pg_restore --clean --if-exists --no-owner -d snowline_pm <dump>
```

**Manual run / reload.** `bash ops/hub/snowline-backup.sh` takes a backup
now (as the owner — never with sudo, or the lock file ends up root-owned
and every later run exits 2). To reload the unit after editing the plist:
`launchctl bootout gui/501/dev.snowline.backup; launchctl bootstrap gui/501
~/Library/LaunchAgents/dev.snowline.backup.plist` (no RunAtLoad: the first
timed run is an hour after loading; `launchctl kickstart
gui/501/dev.snowline.backup` fires one now), or just run
`ops/hub/install-backup-agent.sh` as the owner, which reloads if needed and
proves the unit with one run (do not re-run `install-daemons.sh` for this:
it restarts all four services). Do not `kickstart -k` a timer that launchd has put
in its penalty box after repeated spawn failures — it hangs;
bootout/bootstrap instead.
