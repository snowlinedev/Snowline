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

## Stack-sync guard

`check_not_source_hub` (`src/snowline_platform/stack/sync.py`) refuses to
install the packaged stack when unmanaged `dev.snowline.*` plists exist and
there is no `stack.json`. It checks both `~/Library/LaunchAgents` and
`/Library/LaunchDaemons`.

## Auto-cut (hourly patch trains, #258)

The MBP spoke is a warm standby. It is only worth switching to if it carries
the code the hub has been running, so the hub cuts a train on its own whenever
main moves, and the spoke's `stack sync --auto` timer picks it up.

**What fires.** `dev.snowline.autocut` is a system LaunchDaemon (runs as
`seanlynch`, no login needed) with `StartInterval` 3600 and `RunAtLoad`
false. It runs `ops/hub/launchdaemons/run-autocut.sh`, which execs
`.venv/bin/snowline release autocut` from the platform checkout. Not
`uv run`: that would re-sync the venv the running services import. One run:

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
     `/plugins`. Each one must be `up` or self-reported `degraded`. `degraded`
     is accepted because the usual hub cause is pm replication lag to the
     standby while it is powered off, and that is exactly the state this
     timer fixes. Pass `--strict-health` to require `up`.
   - the train's milestone gate answers ok. Autocut refuses if the gate
     *skipped* (could not judge), where `release cut` would only warn.
4. Version: the next **patch** of the recorded train (v0.5.1 to v0.5.2). It
   never bumps the minor. The current minor's milestone is achieved and the
   next one is open, so a patch is the only train a timer may cut. If a tag is
   already taken at a different sha, autocut skips that version.
5. Runs the ordinary `cut()` in-process, about 6 minutes: tests, builds,
   smoke, tag, publish.
6. Only after the cut succeeds, it commits `release/train.json` (only that
   path) on main as `release: train vX.Y.Z (autocut)` and pushes. If the push
   is rejected (main moved during the cut), it undoes the commit and leaves
   the file uncommitted. The next run lands it as
   `... (autocut: record recovered)`, and does the same for a record left by
   a manual `release cut` run without `--commit-manifest`.

A failed cut never leaves a committed record. Autocut never prompts and never
passes `--force`.

**Where to look.**

- Report: `~/Library/Application Support/Snowline/autocut-report.json`, next to
  the spoke's `sync-report.json` and `bootstrap-report.json`. It holds the
  latest run's `outcome` (`noop` / `ok` / `gated` / `failed`), `detail`,
  per-component `recorded_sha` / `origin_sha` / `moved`, the `gates`, and
  `last_cut` (`version`, `shas`, `at`, `outcome`). `last_cut` carries over
  across noop runs, so it always answers "last cut".
- History: `autocut-history/` gets one entry per run that wasn't a noop.
- Log: `~/Library/Logs/snowline-autocut.log`.
- Exit codes: 0 noop/ok/locked, 2 gated, 1 failed.

**Try it by hand:**
`SNOWLINE_PLATFORM_URL=http://100.81.176.75:8850 .venv/bin/snowline release autocut --dry-run`.
A dry run decides, gates, and plans. It builds, commits, and reports nothing.

**Disable.**

- Without sudo: `touch ~/.config/snowline/autocut.disabled`. The wrapper then
  exits 0 on every fire. `rm` the file to re-enable.
- Fully: `sudo launchctl bootout system/dev.snowline.autocut`, or
  `sudo ops/hub/uninstall-daemons.sh`, which also removes the services'
  daemons.

**Credentials.** The cut pushes tags, creates releases, and pushes the record
commit. On the hub, `gh`'s token lives in `~/.config/gh/hosts.yml` (mode 600,
not the keychain), and git's github.com credential helper is
`gh auth git-credential`. A daemon running as `seanlynch` with `HOME` set can
reach both without a login session. If gh is ever re-authenticated into the
keychain, the `gh_auth` gate refuses every run (exit 2, in the report). The
fix is `gh auth login --insecure-storage`, or moving the plist to a gui/501
LaunchAgent.

**Install.** `sudo ops/hub/install-daemons.sh` (idempotent) copies the plist
to `/Library/LaunchDaemons` and runs `launchctl bootstrap` on it. It does
**not** kickstart it, because a kickstart would start a cut immediately. The
first run happens up to an hour later. To cut now, run
`sudo launchctl kickstart system/dev.snowline.autocut`.
