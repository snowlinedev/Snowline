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
   `/Library/LaunchDaemons` (root:wheel, 644), `launchctl bootstrap system`,
   then `launchctl kickstart -k system/dev.snowline.<svc>`.
4. Polls the four `/health` URLs for up to 60s and prints UP/DOWN.

It then installs the timers from `ops/hub/launchdaemons/` the same way, but
only bootstraps them (never kickstarts): `dev.snowline.autocut` (below) and
`dev.snowline.backup` ("DB backups", below). The backup timer had a per-user
LaunchAgent before this move, so the install also boots that agent out and
moves its plist aside, exactly as for a service; the uninstall restores it.

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

`sudo ops/hub/uninstall-daemons.sh` boots out the system daemons, removes the
plists from `/Library/LaunchDaemons`, restores the newest `.disabled-*` agent
plists and bootstraps them back into `gui/501`.

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
is dumped every hour into iCloud Drive. The unit is a system LaunchDaemon
(installed by `install-daemons.sh`, runs as `UserName=seanlynch`) like the
services, so the timer survives a reboot without anyone re-loading it; the
dumps themselves still need the owner's session, because Postgres is the
owner's per-user agent and iCloud syncs per session — after a FileVault boot
that session always exists. Before the move it was a login-only LaunchAgent.
The install waits for the unit's first (RunAtLoad) dump and fails if it did
not exit 0; a rollback with `uninstall-daemons.sh` restores the agent plist,
and leaves the daemon in place if there is none to restore.

**What runs.** `ops/hub/launchdaemons/dev.snowline.backup.plist` fires
`ops/hub/snowline-backup.sh` every 3600 s and once on load. The script:

- waits up to 120 s for `pg_isready` (Postgres is the owner's per-user
  LaunchAgent, so at boot it can start after this daemon fires), then dumps
  `snowline_platform`, `snowline_pm`, `snowline_governance`,
  `snowline_memory`, `snowline_musher` and `snowline_marketing` with
  `pg_dump -Fc`, verifies each archive with `pg_restore -l`, then renames
  `.partial` to the final name — one store failing does not skip the others,
  and the run exits 1 if any did;
- cross-checks that list against every `*_DATABASE_URL` in
  `~/.config/snowline/*.env` and in the `dev.snowline.*` plists
  (`*_TEST_*` / `*_DRYRUN_*` URLs are ignored); with the default list, a
  configured store that is missing is logged as a WARNING and fails the run,
  so a new service cannot go unbacked-up silently. Setting
  `SNOWLINE_BACKUP_DBS` explicitly is a deliberate exclusion and only notes it;
- refuses to run if the iCloud Drive root is missing (`mkdir -p` would
  otherwise recreate the folder locally and every run would "succeed" while
  nothing left the machine);
- writes owner-only (umask 077, destination 0700): the platform dump carries
  the live replication HMAC secrets, so a dump is as sensitive as the hub;
- prunes each store's dumps older than `SNOWLINE_BACKUP_RETENTION_DAYS`
  (30), including iCloud-evicted `.NAME.icloud` placeholders; the legacy
  `snowline_dev-*` dumps are never touched.

Dumps land in `~/Library/Mobile Documents/com~apple~CloudDocs/Snowline/db-backups/`
as `<store>-<YYYYMMDD-HHMMSS>Z.dump` (UTC). Log: `~/Library/Logs/snowline-backup.log`.
The `.env` secrets are deliberately not backed up.

**Is it alive?** Do this whenever you touch the hub — the job has silently
died twice (2026-07-09 to 09-04 after the repo move; 2026-09-13 to 10-23
while the script existed only on an unmerged branch):

```sh
sudo launchctl print system/dev.snowline.backup | grep -E 'last exit code|runs ='
ls -t ~/Library/Mobile\ Documents/com~apple~CloudDocs/Snowline/db-backups | head -6
```

`last exit code = 0` and a dump under an hour old is healthy. `78: EX_CONFIG`
means launchd could not spawn the program (script path missing — check the
checkout). A non-zero exit with log lines says which store failed.

**Restore.** To inspect first (recommended):

```sh
createdb snowline_restore
pg_restore --no-owner -d snowline_restore <dump>
```

To overwrite the live store (destructive; stop that service first):

```sh
pg_restore --clean --if-exists --no-owner -d snowline_pm <dump>
```

**Manual run / reload.** `bash ops/hub/snowline-backup.sh` takes a backup now.
To restart the unit after editing the plist: `sudo launchctl bootout
system/dev.snowline.backup && sudo launchctl bootstrap system
/Library/LaunchDaemons/dev.snowline.backup.plist` (the bootstrap itself takes
a backup). Do not `kickstart -k` a timer that launchd has put in its penalty
box after repeated spawn failures — it hangs; bootout/bootstrap instead.
