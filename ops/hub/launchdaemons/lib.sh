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

# ---- unit run locks ----
# A timer that must not be booted out mid-run holds a flock on
# $LOCKS_DIR/<name>.lock for its whole run: autocut for the cut (taken by
# `snowline release autocut`), the backup for the dump (snowline-backup.sh
# re-executes itself under a flock-holding wrapper). lock_held <path> probes
# a lock without taking it. The kernel drops a flock with its process, so a
# file left by a crash or power loss is simply not held: no pid to trust,
# nothing to take over, no false "in progress" after a reboot.
LOCKS_DIR="$OWNER_HOME/Library/Application Support/Snowline"
BACKUP_LOCK="$LOCKS_DIR/backup.lock"
ICLOUD_ROOT="$OWNER_HOME/Library/Mobile Documents/com~apple~CloudDocs"
BACKUP_DIR="$ICLOUD_ROOT/Snowline/db-backups"   # the backup script's default destination
# Probes with a SHARED non-blocking lock (as autocut's own lock_state does):
# it fails only while an exclusive holder runs, and never leaves the probe
# owning the exclusive lock for an instant.
lock_held() {
  [ -e "$1" ] || return 1
  /usr/bin/python3 -c 'import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
except OSError:
    sys.exit(0)
sys.exit(1)' "$1"
}
# unit_busy_wait <name> [bound_s] — wait (default up to 10 min) while
# <name>.lock is held; the ops scripts call it right before booting that unit
# out. Returns 1 if still held after the bound (the caller then skips the
# unit rather than killing a run). A bound of 0 is a plain check. A unit
# with no lock file is never busy.
unit_busy_wait() {
  lock="$LOCKS_DIR/$1.lock"
  bound="${2:-600}"
  busy_waited=0
  while lock_held "$lock"; do
    if [ "$busy_waited" -ge "$bound" ]; then
      echo "   $1 is still running after ${busy_waited}s ($lock held)" >&2
      return 1
    fi
    echo "   $1 run in progress; waiting (${busy_waited}s)"
    sleep 5
    busy_waited=$((busy_waited + 5))
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
AUTOCUT_LOCK="$LOCKS_DIR/autocut.lock"
AUTOCUT_LOG="$OWNER_HOME/Library/Logs/snowline-autocut.log"
autocut_lock_held() { lock_held "$AUTOCUT_LOCK"; }
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
# domain. The agent plist comes from, in order: a live one already in
# ~/Library/LaunchAgents (kept as is), the newest `.disabled-*` copy (moved
# back), or the tracked copy under ops/hub/launchagents/ (installed fresh).
# Order of operations: the agent is LOADED FIRST, and only then is the system
# daemon booted out and its plist removed — labels are per domain, so a brief
# overlap is harmless (the services bind the same ports only after the
# daemon is gone; the backup's flock makes an overlapping fire step aside),
# and a failed agent load leaves the daemon running instead of leaving
# nothing. No kickstart: a timer's RunAtLoad is enough, a service is
# kickstarted by the caller. Returns 0 when the agent is loaded and the
# daemon removed.
# With `keep-daemon-if-none` and no agent plist from any source, the daemon
# is left in place and the function returns 1 (the backup timer: a rollback
# can never leave the hub without the unit). Without it (the services) the
# daemon is removed even when no agent could be found, as a rollback must.
restore_agent() {
  label="dev.snowline.$1"
  disabled="$(ls -1 "$AGENTS/$label.plist.disabled-"* 2>/dev/null | sort | tail -n 1)"
  tracked="$LAUNCHAGENTS_SRC/$label.plist"
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
  elif [ "${2:-}" = keep-daemon-if-none ]; then
    echo "   no agent plist to restore for $label (no .disabled-*, none live, none tracked); LEAVING the system daemon in place" >&2
    return 1
  else
    echo "   no agent plist for $label from any source; removing the daemon anyway" >&2
    launchctl bootout "system/$label" 2>/dev/null || true
    rm -f "${DAEMONS_DIR:?}/${label:?}.plist"
    return 1
  fi
  # 1. load the agent (an already-loaded one counts)
  if ! agent_loaded "$1" && ! launchctl bootstrap "gui/$OWNER_UID" "$AGENTS/$label.plist" && ! agent_loaded "$1"; then
    if [ "${2:-}" = keep-daemon-if-none ]; then
      echo "   agent plist for $label is on disk but could NOT be loaded into gui/$OWNER_UID (no login session?); LEAVING the system daemon in place; the agent loads at the next login" >&2
      return 1
    fi
    echo "   agent plist for $label is on disk but could NOT be loaded into gui/$OWNER_UID (no login session?); it loads at the next login — removing the daemon as asked, nothing runs for $label until then" >&2
  fi
  # 2. now retire the daemon
  launchctl bootout "system/$label" 2>/dev/null || true
  wait_unloaded "system/$label" || echo "   $label still registered after 30s; continuing" >&2
  rm -f "${DAEMONS_DIR:?}/${label:?}.plist"
  agent_loaded "$1"
}
