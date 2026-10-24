#!/bin/sh
# Shared constants and helpers for the hub's launchd ops scripts. Sourced (not
# executed) by ops/hub/install-daemons.sh, ops/hub/uninstall-daemons.sh,
# ops/hub/launchdaemons/run-service.sh and ops/hub/snowline-backup.sh. POSIX sh.

# ---- the hub's units (one definition for install and uninstall) ----
# The four services: system LaunchDaemons, kickstarted after bootstrap.
SERVICES="platform pm governance memory"
# System-only timers: bootstrapped, never kickstarted (a kickstart fires an
# immediate run — for autocut, a ~6-minute cut). No per-user predecessor.
SYSTEM_TIMERS="autocut"
# Timers that replaced a per-user LaunchAgent of the same label and prove
# themselves on load (RunAtLoad): the install waits for and judges that first
# run, and puts the agent back if the daemon cannot run at all; the uninstall
# restores the agent, or leaves the daemon when there is nothing to restore.
# Their agent plists are tracked under ops/hub/launchagents/.
AGENT_TIMERS="backup"

OWNER_UID=501
OWNER_HOME=/Users/seanlynch
DAEMONS_DIR=/Library/LaunchDaemons           # not DEST: the backup script's dump dir
AGENTS="$OWNER_HOME/Library/LaunchAgents"
OPS_HUB="$(cd "$(dirname "$0")" && pwd)"      # the calling script's directory
case "$OPS_HUB" in */launchdaemons) OPS_HUB="${OPS_HUB%/launchdaemons}" ;; esac
LAUNCHDAEMONS_SRC="$OPS_HUB/launchdaemons"
LAUNCHAGENTS_SRC="$OPS_HUB/launchagents"

# ---- logging ----
# Helpers log through `log`. A caller that defines its own log() FUNCTION
# before sourcing (run-service.sh prefixes its lines) keeps it; otherwise a
# default UTC-stamped one is defined here. Tested with `type` (not
# `command -v`, which finds /usr/bin/log on macOS) at source time, with no
# `head` in the pipe so pipefail callers cannot see a SIGPIPE.
if ! type log 2>/dev/null | grep -q 'function'; then
  log() { echo "$(date -u +%FT%TZ) $*"; }
fi

# ---- waits ----
# wait_for_postgres [host] — bounded wait (120 s, 5 s steps) for pg_isready;
# returns 0 when ready, 1 after the bound (callers go on anyway: launchd's
# KeepAlive, or the next timer tick, retries). Postgres on the hub is the
# owner's per-user LaunchAgent, so anything that can start before the login
# session must wait for it.
wait_for_postgres() {
  if [ -n "${1:-}" ]; then set -- -h "$1"; else set --; fi
  waited=0
  until pg_isready -q "$@"; do
    if [ "$waited" -ge 120 ]; then
      log "postgres not ready after ${waited}s; continuing anyway (launchd / the next tick retries)"
      return 1
    fi
    log "waiting for postgres (${waited}s)"
    sleep 5
    waited=$((waited + 5))
  done
  return 0
}

# backup_lock_held — is a backup run in progress? snowline-backup.sh holds
# BACKUP_LOCK (a directory, created atomically with mkdir, holding its pid)
# for its whole run and removes it on exit; a stale lock from a crashed run
# (pid gone) does not count.
BACKUP_LOCK="$OWNER_HOME/Library/Application Support/Snowline/backup.lock"
backup_lock_held() {
  [ -d "$BACKUP_LOCK" ] || return 1
  pid="$(cat "$BACKUP_LOCK/pid" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}
# backup_guard — wait (up to 10 min) for a running backup to finish before
# anything boots its unit out: a SIGTERM mid-dump leaves a .partial in iCloud
# and loses that hour's dump of the store it was on. Call it right before the
# bootout, not minutes earlier. After the bound it returns 1 and the caller
# proceeds (and says so).
backup_guard() {
  waited=0
  while backup_lock_held; do
    if [ "$waited" -ge 600 ]; then
      echo "   a backup run is still going after ${waited}s; proceeding anyway (that hour's dump may be lost)" >&2
      return 1
    fi
    echo "   backup run in progress (pid $pid); waiting (${waited}s)"
    sleep 5
    waited=$((waited + 5))
  done
  return 0
}

# wait_unloaded <domain/label> — after a bootout, wait (up to 30 s) until
# launchd no longer knows the label, so an immediate bootstrap of the same
# label cannot race the teardown ("Operation already in progress").
wait_unloaded() {
  waited=0
  while launchctl print "$1" >/dev/null 2>&1; do
    [ "$waited" -ge 30 ] && return 1
    sleep 1
    waited=$((waited + 1))
  done
  return 0
}

# ---- autocut guard ----
# autocut_guard [--wait] — refuse (exit 75) while an autocut is running: a
# `launchctl bootout system/dev.snowline.autocut` SIGTERMs a running cut (~6
# min, hourly). The cut holds a flock on AUTOCUT_LOCK for its whole run, so
# test it first. `--wait` polls up to 15 min instead. (A fire that starts
# between this check and the bootout is not covered; the window is seconds.)
AUTOCUT_LOCK="$OWNER_HOME/Library/Application Support/Snowline/autocut.lock"
AUTOCUT_LOG="$OWNER_HOME/Library/Logs/snowline-autocut.log"
autocut_lock_held() {
  [ -e "$AUTOCUT_LOCK" ] || return 1
  /usr/bin/python3 -c 'import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(0)
sys.exit(1)' "$AUTOCUT_LOCK"
}
autocut_guard() {
  waited=0
  while autocut_lock_held; do
    if [ "${1:-}" != "--wait" ] || [ "$waited" -ge 900 ]; then
      echo "refusing: an autocut is running (lock held: $AUTOCUT_LOCK)." >&2
      echo "  booting the timer out now would kill the cut mid-publish." >&2
      echo "  follow it in $AUTOCUT_LOG, then re-run (or pass --wait)." >&2
      exit 75
    fi
    echo "   autocut running (lock held); waiting (${waited}s)"
    sleep 15
    waited=$((waited + 15))
  done
}

# ---- per-user agent restore ----
# agent_loaded <name> — is the unit loaded in the owner's gui domain?
agent_loaded() {
  launchctl print "gui/$OWNER_UID/dev.snowline.$1" >/dev/null 2>&1
}

# restore_agent <name> [keep-daemon-if-none] — put a unit back in the per-user
# domain: boot the system daemon out, remove its plist, then load the agent
# plist found in this order: the newest `.disabled-*` copy (moved back), a
# live one already in ~/Library/LaunchAgents, or the tracked copy under
# ops/hub/launchagents/ (installed fresh). No kickstart: a timer's RunAtLoad
# is enough, a service is kickstarted by the caller if wanted. An agent that
# is already loaded counts as success. Returns 0 when an agent is loaded.
# With `keep-daemon-if-none` and no agent plist from ANY source, the system
# daemon is LEFT IN PLACE and the function returns 1 — for the backup timer,
# so a rollback can never leave the hub without the unit. Without it (the
# services) the daemon is removed regardless, as a rollback must.
restore_agent() {
  label="dev.snowline.$1"
  disabled="$(ls -1 "$AGENTS/$label.plist.disabled-"* 2>/dev/null | sort | tail -n 1)"
  tracked="$LAUNCHAGENTS_SRC/$label.plist"
  if [ -z "$disabled" ] && [ ! -f "$AGENTS/$label.plist" ] && [ ! -f "$tracked" ] \
     && [ "${2:-}" = keep-daemon-if-none ]; then
    echo "   no agent plist to restore for $label (no .disabled-*, none live, none tracked); LEAVING the system daemon in place" >&2
    return 1
  fi
  launchctl bootout "system/$label" 2>/dev/null || true
  wait_unloaded "system/$label" || echo "   $label still registered after 30s; continuing" >&2
  rm -f "${DAEMONS_DIR:?}/${label:?}.plist"
  # A live agent plist (put there by hand since the daemon move) wins over
  # the older .disabled-* copy; the copy is only moved back when nothing
  # else is there.
  if [ -f "$AGENTS/$label.plist" ]; then
    [ -z "$disabled" ] || echo "   keeping the live $AGENTS/$label.plist (leaving $disabled aside)"
  elif [ -n "$disabled" ]; then
    mv "$disabled" "$AGENTS/$label.plist"
    chown "$OWNER_UID" "$AGENTS/$label.plist"
    echo "   restored $AGENTS/$label.plist"
  elif [ -f "$tracked" ]; then
    cp "$tracked" "$AGENTS/$label.plist"
    chown "$OWNER_UID" "$AGENTS/$label.plist"
    chmod 644 "$AGENTS/$label.plist"
    echo "   installed the tracked agent plist $tracked -> $AGENTS/$label.plist"
  fi
  if [ ! -f "$AGENTS/$label.plist" ]; then
    echo "   no agent plist for $label; daemon removed, nothing loaded" >&2
    return 1
  fi
  if agent_loaded "$1"; then
    echo "   agent $label already loaded in gui/$OWNER_UID"
    return 0
  fi
  if launchctl bootstrap "gui/$OWNER_UID" "$AGENTS/$label.plist" || agent_loaded "$1"; then
    return 0
  fi
  echo "   agent plist for $label is on disk but could NOT be loaded into gui/$OWNER_UID (no login session?); it loads at the next login — nothing is running for $label until then" >&2
  return 1
}
