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
