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
DAEMONS_DIR="$DEST"
AGENTS="$OWNER_HOME/Library/LaunchAgents"
STAMP="$(date +%Y%m%d)"

# shellcheck disable=SC1091
. "$SRC/lib.sh"
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

fail=0
for svc in $SERVICES; do
  install_unit "$svc" yes || fail=1   # the /health poll below reports it too
done

backup_loaded=yes
for timer in $TIMERS; do
  if ! install_unit "$timer" no; then
    fail=1
    [ "$timer" = backup ] && backup_loaded=no
  fi
done

echo "== waiting up to 60s for /health"
urls="platform|http://100.81.176.75:8850/health pm|http://127.0.0.1:8802/health governance|http://127.0.0.1:8801/health memory|http://127.0.0.1:8803/health"
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
# run to finish — it may itself wait up to 120s for Postgres, then dump every
# store, so allow 5 minutes. Exit 0 = dumps landed. A non-zero exit = the
# daemon cannot dump (e.g. the system domain lacks access to the owner's
# iCloud folder, see docs/ops/hub-daemons.md "DB backups"): put the per-user
# agent back if one exists (restore_agent keeps the daemon when there is
# none) and fail. A run still in progress after the wait is reported and
# fails the install but is NOT rolled back — booting a healthy in-flight
# dump out would be worse than asking the operator to look.
echo "== dev.snowline.backup (RunAtLoad dump)"
code=""
i=0
if [ "$backup_loaded" = yes ]; then
  while [ "$i" -lt 300 ]; do
    code="$(launchctl print system/dev.snowline.backup 2>/dev/null | sed -n 's/.*last exit code = \([0-9]*\).*/\1/p' | head -n 1)"
    [ -n "$code" ] && break
    sleep 2
    i=$((i + 2))
  done
  tail -n 8 "$OWNER_HOME/Library/Logs/snowline-backup.log" 2>/dev/null | sed 's/^/   /'
fi
if [ "$code" = 0 ]; then
  echo "   backup: OK (exit 0)"
elif [ "$backup_loaded" = yes ] && [ -z "$code" ]; then
  echo "   backup: still running after ${i}s; not rolling back — check $OWNER_HOME/Library/Logs/snowline-backup.log and 'launchctl print system/dev.snowline.backup'" >&2
  fail=1
else
  if [ "$backup_loaded" != yes ]; then
    echo "   backup: FAILED (daemon did not bootstrap)" >&2
  else
    echo "   backup: FAILED (exit $code) — see $OWNER_HOME/Library/Logs/snowline-backup.log" >&2
  fi
  restore_agent backup keep-daemon-if-none || true
  fail=1
fi
exit "$fail"
