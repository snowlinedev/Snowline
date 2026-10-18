#!/bin/sh
# Reverse ops/hub/install-daemons.sh: remove the system LaunchDaemons and put
# the per-user LaunchAgents back (#239). Run:  sudo ops/hub/uninstall-daemons.sh [--wait]
# Refuses (exit 75) while an autocut is running; --wait waits up to 15 min.

[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

SERVICES="platform pm governance memory"
TIMERS="autocut"  # system-only; no LaunchAgent predecessor to restore
OWNER_UID=501
OWNER_HOME=/Users/seanlynch
DEST=/Library/LaunchDaemons
AGENTS="$OWNER_HOME/Library/LaunchAgents"

# --- autocut in-flight guard (#258) ---
# `launchctl bootout system/dev.snowline.autocut` SIGTERMs a running cut (~6
# min, hourly). The cut holds a flock on AUTOCUT_LOCK for its whole run, so
# test it first and refuse while it is held. `--wait` polls up to 15 min
# instead. (A fire that starts between this check and the bootout is not
# covered; the window is a few seconds.)
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
autocut_guard "$@"

for timer in $TIMERS; do
  label="dev.snowline.$timer"
  echo "== $label"
  launchctl bootout "system/$label" 2>/dev/null || true
  rm -f "$DEST/$label.plist"
done

for svc in $SERVICES; do
  label="dev.snowline.$svc"
  echo "== $label"
  launchctl bootout "system/$label" 2>/dev/null || true
  rm -f "$DEST/$label.plist"
  # restore the newest .disabled-* agent plist, if any
  disabled="$(ls -1 "$AGENTS/$label.plist.disabled-"* 2>/dev/null | sort | tail -n 1)"
  if [ -n "$disabled" ]; then
    mv "$disabled" "$AGENTS/$label.plist"
    chown "$OWNER_UID" "$AGENTS/$label.plist"
    echo "   restored $AGENTS/$label.plist"
  else
    echo "   no .disabled-* agent plist found for $label" >&2
  fi
  if [ -f "$AGENTS/$label.plist" ]; then
    launchctl bootstrap "gui/$OWNER_UID" "$AGENTS/$label.plist" || true
    launchctl kickstart -k "gui/$OWNER_UID/$label" || true
  fi
done
echo "done; check the four /health URLs (docs/ops/hub-daemons.md)"
