#!/bin/sh
# Reverse ops/hub/install-daemons.sh: remove the system LaunchDaemons and put
# the per-user LaunchAgents back (#239). Run:  sudo ops/hub/uninstall-daemons.sh [--wait]
# Refuses (exit 75) while an autocut is running; --wait waits up to 15 min.
# Unit lists and helpers: launchdaemons/lib.sh.

[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

# shellcheck disable=SC1091
. "$(cd "$(dirname "$0")" && pwd)/launchdaemons/lib.sh"
autocut_guard "$@"

# System-only timers: no LaunchAgent predecessor; just remove.
for timer in $SYSTEM_TIMERS; do
  label="dev.snowline.$timer"
  echo "== $label"
  launchctl bootout "system/$label" 2>/dev/null || true
  rm -f "${DAEMONS_DIR:?}/${label:?}.plist"
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

# Agent-backed timers (the backup): same, never kickstarted (RunAtLoad is
# enough), waiting for a running dump first; if no agent plist exists from
# any source the daemon is LEFT IN PLACE — a rollback must not leave the hub
# without a backup unit.
for timer in $AGENT_TIMERS; do
  echo "== dev.snowline.$timer"
  backup_guard || true
  restore_agent "$timer" keep-daemon-if-none || true
done
echo "done; check the four /health URLs and 'launchctl print gui/$OWNER_UID/dev.snowline.backup' (docs/ops/hub-daemons.md)"
