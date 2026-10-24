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

# Services and the backup timer: restore_agent (lib.sh) puts the newest
# .disabled-* agent plist back and loads it, or leaves the daemon in place
# when there is nothing to restore (never leave the hub without the unit).
# A service is then kickstarted; a timer is not (RunAtLoad is enough).
for svc in $SERVICES $AGENT_TIMERS; do
  echo "== dev.snowline.$svc"
  if restore_agent "$svc"; then
    case " $AGENT_TIMERS " in
      *" $svc "*) ;;
      *) launchctl kickstart -k "gui/$OWNER_UID/dev.snowline.$svc" || true ;;
    esac
  fi
done
echo "done; check the four /health URLs and 'launchctl print gui/$OWNER_UID/dev.snowline.backup' (docs/ops/hub-daemons.md)"
