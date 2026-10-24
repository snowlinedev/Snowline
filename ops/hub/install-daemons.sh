#!/bin/sh
# Move the hub's four services (and the hourly DB backup timer) from per-user
# LaunchAgents to system LaunchDaemons so they run at boot without a login
# (#239 / 239ec238; backup: decision c68ec655, item f304ec06).
# Idempotent. Run:  sudo ops/hub/install-daemons.sh [--wait]
# Refuses (exit 75) while an autocut is running; --wait waits up to 15 min.
# Rollback:        sudo ops/hub/uninstall-daemons.sh
# See docs/ops/hub-daemons.md.

[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

SERVICES="platform pm governance memory"
# Interval timers (#258): installed + bootstrapped, never kickstarted — a
# kickstart would fire an immediate run (for autocut, a ~6-minute cut).
TIMERS="autocut"
# Timers that replace a per-user LaunchAgent of the same label (the backup job
# predates the daemon move). Bootstrapped like TIMERS, and additionally the
# old agent is booted out and its plist moved aside — a timer loaded in both
# domains would dump twice an hour. RunAtLoad on the backup plist means the
# bootstrap itself takes one backup, which proves the path end to end.
AGENT_TIMERS="backup"
OWNER_UID=501
OWNER_HOME=/Users/seanlynch
SRC="$(cd "$(dirname "$0")" && pwd)/launchdaemons"
DEST=/Library/LaunchDaemons
AGENTS="$OWNER_HOME/Library/LaunchAgents"
STAMP="$(date +%Y%m%d)"

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

for svc in $SERVICES; do
  label="dev.snowline.$svc"
  echo "== $label"
  # 1. stop the agent (ignore: it may not be loaded) ...
  launchctl bootout "gui/$OWNER_UID/$label" 2>/dev/null || true
  # 2. ... and move its plist aside so it cannot reload at login and double-bind
  if [ -f "$AGENTS/$label.plist" ]; then
    mv "$AGENTS/$label.plist" "$AGENTS/$label.plist.disabled-$STAMP"
    echo "   agent plist -> $label.plist.disabled-$STAMP"
  fi
  # 3. install the daemon plist (replace a previously loaded daemon cleanly)
  launchctl bootout "system/$label" 2>/dev/null || true
  cp "$SRC/$label.plist" "$DEST/$label.plist"
  chown root:wheel "$DEST/$label.plist"
  chmod 644 "$DEST/$label.plist"
  launchctl bootstrap system "$DEST/$label.plist"
  launchctl kickstart -k "system/$label"
done

for timer in $TIMERS $AGENT_TIMERS; do
  label="dev.snowline.$timer"
  echo "== $label (timer; not kickstarted)"
  case " $AGENT_TIMERS " in
    *" $timer "*)
      launchctl bootout "gui/$OWNER_UID/$label" 2>/dev/null || true
      if [ -f "$AGENTS/$label.plist" ]; then
        mv "$AGENTS/$label.plist" "$AGENTS/$label.plist.disabled-$STAMP"
        echo "   agent plist -> $label.plist.disabled-$STAMP"
      fi
      ;;
  esac
  launchctl bootout "system/$label" 2>/dev/null || true
  cp "$SRC/$label.plist" "$DEST/$label.plist"
  chown root:wheel "$DEST/$label.plist"
  chmod 644 "$DEST/$label.plist"
  launchctl bootstrap system "$DEST/$label.plist"
done

echo "== waiting up to 60s for /health"
urls="platform|http://100.81.176.75:8850/health pm|http://127.0.0.1:8802/health governance|http://127.0.0.1:8801/health memory|http://127.0.0.1:8803/health"
fail=0
for entry in $urls; do
  name="${entry%%|*}"
  url="${entry#*|}"
  i=0
  ok=no
  while [ "$i" -lt 60 ]; do
    if curl -fsS -m 2 -o /dev/null "$url" 2>/dev/null; then ok=yes; break; fi
    sleep 1
    i=$((i + 1))
  done
  echo "   $name $url: $([ "$ok" = yes ] && echo UP || echo DOWN)"
  [ "$ok" = yes ] || fail=1
done

# The backup timer's RunAtLoad dump: give it a moment, then show the verdict.
echo "== dev.snowline.backup (RunAtLoad dump)"
sleep 5
launchctl print system/dev.snowline.backup 2>/dev/null | grep -E "last exit code|runs =" | sed 's/^/   /'
tail -n 6 "$OWNER_HOME/Library/Logs/snowline-backup.log" 2>/dev/null | sed 's/^/   /'
exit "$fail"
