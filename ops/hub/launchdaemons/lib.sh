#!/bin/sh
# Shared constants and helpers for the hub's launchd ops scripts. Sourced (not
# executed) by ops/hub/install-daemons.sh, ops/hub/uninstall-daemons.sh and
# ops/hub/snowline-backup.sh. POSIX sh. (run-service.sh, the services' boot
# wrapper, deliberately does NOT source this: it keeps its own inline
# Postgres wait so it depends on no file but itself.)

# ---- the hub's units (one definition for install and uninstall) ----
# The four services: system LaunchDaemons, kickstarted after bootstrap.
SERVICES="platform pm governance memory"
# System-only timers: bootstrapped, never kickstarted (a kickstart fires an
# immediate run — for autocut, a ~6-minute cut). No per-user predecessor.
TIMERS="autocut"
# The hourly DB backup is a per-user LaunchAgent on purpose (see
# docs/ops/hub-daemons.md "DB backups"); its tracked plist lives under
# ops/hub/launchagents/ and install-daemons.sh installs it.

OWNER_UID=501
# The hub owner's home. The root-run ops scripts need it fixed; the backup
# script sets SNOWLINE_OWNER_HOME=$HOME before sourcing so that, run by any
# user anywhere (a developer testing it), its lock and default destination
# are under that user's home.
OWNER_HOME="${SNOWLINE_OWNER_HOME:-/Users/seanlynch}"
DAEMONS_DIR=/Library/LaunchDaemons           # not DEST: the backup script's dump dir
AGENTS="$OWNER_HOME/Library/LaunchAgents"
OPS_HUB="$(cd "$(dirname "$0")" && pwd)"      # the calling script's directory
case "$OPS_HUB" in */launchdaemons) OPS_HUB="${OPS_HUB%/launchdaemons}" ;; esac
LAUNCHDAEMONS_SRC="$OPS_HUB/launchdaemons"
LAUNCHAGENTS_SRC="$OPS_HUB/launchagents"
ICLOUD_ROOT="$OWNER_HOME/Library/Mobile Documents/com~apple~CloudDocs"
BACKUP_DIR="$ICLOUD_ROOT/Snowline/db-backups"   # the backup script's default destination

# ---- logging ----
# Helpers log through `log`. A caller that defines its own log() FUNCTION
# before sourcing keeps it; otherwise a default UTC-stamped one is defined
# here. Tested with `type` (not `command -v`, which finds /usr/bin/log on
# macOS) at source time, with no `head` in the pipe so pipefail callers
# cannot see a SIGPIPE.
if ! type log 2>/dev/null | grep -q 'function'; then
  log() { echo "$(date -u +%FT%TZ) $*"; }
fi

# ---- waits ----
# wait_for_postgres [host] — bounded wait (120 s, 5 s steps) for pg_isready;
# returns 0 when ready, 1 after the bound (callers go on anyway: the next
# timer tick retries). Postgres on the hub is the owner's per-user
# LaunchAgent, so anything that can start before the login session must
# wait for it.
wait_for_postgres() {
  if [ -n "${1:-}" ]; then set -- -h "$1"; else set --; fi
  waited=0
  until pg_isready -q "$@"; do
    if [ "$waited" -ge 120 ]; then
      log "postgres not ready after ${waited}s; continuing anyway (the next tick retries)"
      return 1
    fi
    log "waiting for postgres (${waited}s)"
    sleep 5
    waited=$((waited + 5))
  done
  return 0
}

# unit_running <domain/label> — is a run of the unit in progress right now?
unit_running() {
  launchctl print "$1" 2>/dev/null | grep -q -E '^[[:space:]]*(state = running|pid = [0-9])'
}
# unit_last_exit <domain/label> — the numeric exit code of the unit's last
# completed run, or nothing (never ran, or ended by a signal).
unit_last_exit() {
  launchctl print "$1" 2>/dev/null | sed -n 's/.*last exit code = \([0-9][0-9]*\).*/\1/p' | head -n 1
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

# ---- unit run locks ----
# A timer that must not be booted out mid-run holds a flock on
# $LOCKS_DIR/<name>.lock for its whole run: autocut for the cut (taken by
# `snowline release autocut`), the backup for the dump (snowline-backup.sh
# opens the file on fd 9 for its lifetime and flocks it). lock_held <path> probes
# a lock without keeping it (a shared non-blocking lock, released at once;
# the backup's acquire retries over a few seconds so a probe cannot make it
# step aside). The kernel drops a flock with its process, so a file left by
# a crash or power loss is simply not held: no pid to trust, nothing to
# take over, no false "in progress" after a reboot.
LOCKS_DIR="$OWNER_HOME/Library/Application Support/Snowline"
BACKUP_LOCK="$LOCKS_DIR/backup.lock"
AUTOCUT_LOCK="$LOCKS_DIR/autocut.lock"
AUTOCUT_LOG="$OWNER_HOME/Library/Logs/snowline-autocut.log"
# Exit 0 = held, 1 = free; anything else means the probe itself could not
# run (python3 shim without Command Line Tools, unreadable file) and is
# treated as HELD — a guard that cannot tell must refuse, not wave through.
lock_held() {
  [ -e "$1" ] || return 1
  /usr/bin/python3 -c 'import errno, fcntl, os, sys
try:
    fd = os.open(sys.argv[1], os.O_RDONLY)
except OSError:
    sys.exit(2)          # cannot even open it: the probe could not run
try:
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
except OSError as e:
    sys.exit(0 if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN) else 2)
sys.exit(1)' "$1" 2>/dev/null
  case $? in
    0) return 0 ;;
    1) return 1 ;;
    *) echo "   cannot probe the lock $1 (python3 unavailable?); treating it as held" >&2; return 0 ;;
  esac
}
autocut_lock_held() { lock_held "$AUTOCUT_LOCK"; }

# unit_busy_wait <name> [bound_s] — wait (default up to 10 min) while
# <name>.lock is held; the ops scripts call it right before booting that
# unit out. Returns 1 if still held after the bound (the caller then skips
# the unit rather than killing a run). A bound of 0 is a plain check.
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

# ---- autocut guard ----
# autocut_guard [--wait] — refuse (exit 75) while an autocut is running: a
# `launchctl bootout system/dev.snowline.autocut` SIGTERMs a running cut (~6
# min, hourly). The cut holds a flock on AUTOCUT_LOCK for its whole run, so
# test it first. `--wait` polls up to 15 min instead. (A fire that starts
# between this check and the bootout is not covered by this guard; the
# install re-checks the lock right before touching the autocut unit.)
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

# ---- per-user agent restore (the services' rollback) ----
# restore_agent <name> — boot the system daemon out, remove its plist, put
# the newest `.disabled-*` agent plist back (a live one already there is
# kept) and load it into gui/501. Returns 0 when an agent is loaded; the
# caller kickstarts it. In that order on purpose: a service runs its whole
# startup (migrations, registration) before binding its port, so two
# instances must never start together.
restore_agent() {
  label="dev.snowline.$1"
  launchctl bootout "system/$label" 2>/dev/null || true
  wait_unloaded "system/$label" || echo "   $label still registered after 30s; continuing" >&2
  rm -f "${DAEMONS_DIR:?}/${label:?}.plist"
  disabled="$(ls -1 "$AGENTS/$label.plist.disabled-"* 2>/dev/null | sort | tail -n 1)"
  if [ -f "$AGENTS/$label.plist" ]; then
    [ -z "$disabled" ] || echo "   keeping the live $AGENTS/$label.plist (leaving $disabled aside)"
  elif [ -n "$disabled" ]; then
    mv "$disabled" "$AGENTS/$label.plist"
    chown "$OWNER_UID" "$AGENTS/$label.plist"
    echo "   restored $AGENTS/$label.plist"
  else
    echo "   no .disabled-* agent plist found for $label; daemon removed, nothing loaded" >&2
    return 1
  fi
  if launchctl print "gui/$OWNER_UID/$label" >/dev/null 2>&1; then
    echo "   agent $label already loaded in gui/$OWNER_UID"
    return 0
  fi
  if launchctl bootstrap "gui/$OWNER_UID" "$AGENTS/$label.plist"; then
    return 0
  fi
  echo "   agent plist for $label is on disk but could NOT be loaded into gui/$OWNER_UID (no login session?); it loads at the next login — nothing runs for $label until then" >&2
  return 1
}
