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
# Interval timers (#258, c68ec655): installed + bootstrapped, never
# kickstarted — a kickstart would fire an immediate run (for autocut, a
# ~6-minute cut). Any per-user LaunchAgent of the same label is booted out and
# its plist moved aside first (the backup job predates the daemon move; a
# timer loaded in both domains would dump twice an hour) — no-ops for a timer
# that never had one.
TIMERS="autocut backup"
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

# install_unit <name> <kick:yes|no> — the same steps for a service and a timer;
# only the trailing kickstart differs (a timer must never be kickstarted: that
# fires an immediate run). Returns non-zero if the bootstrap failed.
install_unit() {
  label="dev.snowline.$1"
  echo "== $label$([ "$2" = yes ] || echo ' (timer; not kickstarted)')"
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
  if ! launchctl bootstrap system "$DEST/$label.plist"; then
    echo "   bootstrap FAILED for $label" >&2
    return 1
  fi
  [ "$2" = yes ] && launchctl kickstart -k "system/$label"
  return 0
}

# restore_agent <name> — undo install_unit for a unit that had a per-user
# agent: boot the daemon out, put the newest .disabled-* plist back and load
# it. Used when the install of that unit cannot be trusted.
restore_agent() {
  label="dev.snowline.$1"
  launchctl bootout "system/$label" 2>/dev/null || true
  rm -f "${DEST:?}/${label:?}.plist"
  disabled="$(ls -1 "$AGENTS/$label.plist.disabled-"* 2>/dev/null | sort | tail -n 1)"
  if [ -n "$disabled" ]; then
    mv "$disabled" "$AGENTS/$label.plist"
    chown "$OWNER_UID" "$AGENTS/$label.plist"
    launchctl bootstrap "gui/$OWNER_UID" "$AGENTS/$label.plist" || true
    echo "   restored and loaded the per-user agent $AGENTS/$label.plist" >&2
  else
    echo "   no per-user agent plist to restore for $label — NO backup unit is loaded" >&2
  fi
}

for svc in $SERVICES; do
  install_unit "$svc" yes || true   # /health poll below reports it
done

backup_loaded=yes
for timer in $TIMERS; do
  if ! install_unit "$timer" no; then
    [ "$timer" = backup ] && backup_loaded=no
  fi
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

# The backup timer's RunAtLoad dump proves the path end to end: wait for the
# run to finish (it waits up to 120s for Postgres itself) and FAIL the install
# if it exited non-zero — the old agent has already been retired above, so a
# daemon that cannot dump must not pass silently.
echo "== dev.snowline.backup (RunAtLoad dump)"
code=""
i=0
if [ "$backup_loaded" = yes ]; then
  while [ "$i" -lt 150 ]; do
    code="$(launchctl print system/dev.snowline.backup 2>/dev/null | sed -n 's/.*last exit code = \([0-9]*\).*/\1/p' | head -n 1)"
    [ -n "$code" ] && break
    sleep 2
    i=$((i + 2))
  done
  tail -n 8 "$OWNER_HOME/Library/Logs/snowline-backup.log" 2>/dev/null | sed 's/^/   /'
fi
if [ "$code" = "0" ]; then
  echo "   backup: OK (exit 0)"
else
  if [ "$backup_loaded" != yes ]; then
    echo "   backup: FAILED (daemon did not bootstrap)" >&2
  else
    echo "   backup: FAILED (exit ${code:-still running after ${i}s}) — see $OWNER_HOME/Library/Logs/snowline-backup.log" >&2
  fi
  # Do not leave the hub without a loaded backup unit: put the agent back.
  restore_agent backup
  fail=1
fi
exit "$fail"
