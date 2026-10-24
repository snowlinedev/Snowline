#!/bin/sh
# Shared helpers for the hub's launchd ops scripts. Sourced (not executed) by
# ops/hub/install-daemons.sh, ops/hub/uninstall-daemons.sh and
# ops/hub/snowline-backup.sh. POSIX sh. Callers set OWNER_UID / OWNER_HOME /
# DAEMONS_DIR (= /Library/LaunchDaemons) / AGENTS before sourcing where a
# helper needs them (deliberately not the generic name DEST, which the
# backup script uses for its dump directory).

# wait_for_postgres [host] — bounded wait (120 s, 5 s steps) for pg_isready,
# logging progress; returns 0 when ready, 1 after the bound (callers go on
# anyway: launchd's KeepAlive, or the next timer tick, retries). Postgres on
# the hub is the owner's per-user LaunchAgent, so anything that can start
# before the login session must wait for it.
wait_for_postgres() {
  if [ -n "${1:-}" ]; then set -- -h "$1"; else set --; fi
  waited=0
  until pg_isready -q "$@"; do
    if [ "$waited" -ge 120 ]; then
      echo "$(date -u +%FT%TZ) postgres not ready after ${waited}s; continuing anyway" >&2
      return 1
    fi
    echo "$(date -u +%FT%TZ) waiting for postgres (${waited}s)"
    sleep 5
    waited=$((waited + 5))
  done
  return 0
}

# autocut_guard [--wait] — refuse (exit 75) while an autocut is running: a
# `launchctl bootout system/dev.snowline.autocut` SIGTERMs a running cut (~6
# min, hourly). The cut holds a flock on AUTOCUT_LOCK for its whole run, so
# test it first. `--wait` polls up to 15 min instead. (A fire that starts
# between this check and the bootout is not covered; the window is seconds.)
AUTOCUT_LOCK="${OWNER_HOME:-$HOME}/Library/Application Support/Snowline/autocut.lock"
AUTOCUT_LOG="${OWNER_HOME:-$HOME}/Library/Logs/snowline-autocut.log"
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

# restore_agent <name> [keep-daemon-if-none] — put a unit back in the
# per-user domain: boot the system daemon out, remove its plist, move the
# newest `.disabled-*` agent plist back (or use a live agent plist that is
# already there) and load it. No kickstart: a timer's RunAtLoad is enough,
# a service is kickstarted by the caller if wanted. Returns 0 when an agent
# was loaded.
# With the second argument `keep-daemon-if-none`, and NOTHING to restore,
# the system daemon is LEFT IN PLACE and the function returns 1 — for the
# backup timer, so a failed install or a rollback can never leave the hub
# without the unit. Without it (the services), the daemon is removed
# regardless, as a rollback of the daemon move must.
restore_agent() {
  label="dev.snowline.$1"
  disabled="$(ls -1 "$AGENTS/$label.plist.disabled-"* 2>/dev/null | sort | tail -n 1)"
  if [ -z "$disabled" ] && [ ! -f "$AGENTS/$label.plist" ] && [ "${2:-}" = keep-daemon-if-none ]; then
    echo "   no agent plist to restore for $label; LEAVING the system daemon in place" >&2
    return 1
  fi
  launchctl bootout "system/$label" 2>/dev/null || true
  rm -f "${DAEMONS_DIR:?}/${label:?}.plist"
  if [ -n "$disabled" ]; then
    mv "$disabled" "$AGENTS/$label.plist"
    chown "$OWNER_UID" "$AGENTS/$label.plist"
    echo "   restored $AGENTS/$label.plist"
  fi
  if [ -f "$AGENTS/$label.plist" ]; then
    launchctl bootstrap "gui/$OWNER_UID" "$AGENTS/$label.plist" || true
    return 0
  fi
  echo "   no agent plist for $label; daemon removed, nothing loaded" >&2
  return 1
}
