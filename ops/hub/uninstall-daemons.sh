#!/bin/sh
# Reverse ops/hub/install-daemons.sh: remove the system LaunchDaemons and put
# the per-user LaunchAgents back (#239). Run:  sudo ops/hub/uninstall-daemons.sh [--wait]
# Refuses (exit 75) while an autocut is running; --wait waits up to 15 min.

[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

SERVICES="platform pm governance memory"
TIMERS="autocut"  # system-only; no LaunchAgent predecessor to restore
# Timers that had a per-user agent before the daemon move: restore the agent
# if its .disabled-* plist is there; if NOT, leave the daemon in place — a
# rollback must never leave the hub with no backup unit at all.
AGENT_TIMERS="backup"
OWNER_UID=501
OWNER_HOME=/Users/seanlynch
DEST=/Library/LaunchDaemons
DAEMONS_DIR="$DEST"
AGENTS="$OWNER_HOME/Library/LaunchAgents"
SRC="$(cd "$(dirname "$0")" && pwd)/launchdaemons"

# shellcheck disable=SC1091
. "$SRC/lib.sh"
autocut_guard "$@"

for timer in $TIMERS; do
  label="dev.snowline.$timer"
  echo "== $label"
  launchctl bootout "system/$label" 2>/dev/null || true
  rm -f "$DEST/$label.plist"
done

# Services: restore_agent (lib.sh) removes the daemon, puts the newest
# .disabled-* agent plist back (or uses a live one) and loads it; then
# kickstart. The daemon is removed even when there is no agent to restore —
# that is what a rollback of the daemon move means for a service.
for svc in $SERVICES; do
  echo "== dev.snowline.$svc"
  if restore_agent "$svc"; then
    launchctl kickstart -k "gui/$OWNER_UID/dev.snowline.$svc" || true
  fi
done

# The backup timer: same, but never kickstarted (RunAtLoad is enough), and
# if there is no agent plist to restore the daemon is LEFT IN PLACE — a
# rollback must not leave the hub without a backup unit.
for timer in $AGENT_TIMERS; do
  echo "== dev.snowline.$timer"
  restore_agent "$timer" keep-daemon-if-none || true
done
echo "done; check the four /health URLs and 'launchctl print gui/$OWNER_UID/dev.snowline.backup' (docs/ops/hub-daemons.md)"
