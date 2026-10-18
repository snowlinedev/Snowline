#!/bin/sh
# Move the hub's four services from per-user LaunchAgents to system
# LaunchDaemons so they start at boot without a login (#239 / 239ec238).
# Idempotent. Run:  sudo ops/hub/install-daemons.sh
# Rollback:        sudo ops/hub/uninstall-daemons.sh
# See docs/ops/hub-daemons.md.

[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

SERVICES="platform pm governance memory"
# Interval timers (#258): installed + bootstrapped, never kickstarted — a
# kickstart would fire an immediate run (for autocut, a ~6-minute cut).
TIMERS="autocut"
OWNER_UID=501
OWNER_HOME=/Users/seanlynch
SRC="$(cd "$(dirname "$0")" && pwd)/launchdaemons"
DEST=/Library/LaunchDaemons
AGENTS="$OWNER_HOME/Library/LaunchAgents"
STAMP="$(date +%Y%m%d)"

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

for timer in $TIMERS; do
  label="dev.snowline.$timer"
  echo "== $label (timer; not kickstarted)"
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
exit "$fail"
