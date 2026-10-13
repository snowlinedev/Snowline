#!/bin/sh
# Reverse ops/hub/install-daemons.sh: remove the system LaunchDaemons and put
# the per-user LaunchAgents back (#239). Run:  sudo ops/hub/uninstall-daemons.sh

[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

SERVICES="platform pm governance memory"
OWNER_UID=501
OWNER_HOME=/Users/seanlynch
DEST=/Library/LaunchDaemons
AGENTS="$OWNER_HOME/Library/LaunchAgents"

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
