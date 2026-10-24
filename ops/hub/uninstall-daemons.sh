#!/bin/sh
# Reverse ops/hub/install-daemons.sh: remove the system LaunchDaemons and put
# the per-user LaunchAgents back (#239). Run:  sudo ops/hub/uninstall-daemons.sh [--wait]
# Refuses (exit 75) while an autocut is running; --wait waits up to 15 min.
# The hourly DB backup agent is not part of the daemon move and is left as
# it is. Unit lists and helpers: launchdaemons/lib.sh.

[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

# shellcheck disable=SC1091
. "$(cd "$(dirname "$0")" && pwd)/launchdaemons/lib.sh"
autocut_guard "$@"

# System-only timers: no LaunchAgent predecessor; just remove (skipped, with
# a message, if a run started since the guard above).
for timer in $TIMERS; do
  label="dev.snowline.$timer"
  echo "== $label"
  if lock_held "$LOCKS_DIR/$timer.lock"; then
    echo "   $timer is running now; leaving $label as it is — re-run after it finishes" >&2
    continue
  fi
  launchctl bootout "system/$label" 2>/dev/null || true
  rm -f "${DAEMONS_DIR:?}/${label:?}.plist"
done

# Services: restore_agent (lib.sh) removes the daemon, puts the newest
# .disabled-* agent plist back (or keeps a live one) and loads it; then
# kickstart.
for svc in $SERVICES; do
  echo "== dev.snowline.$svc"
  if restore_agent "$svc"; then
    launchctl kickstart -k "gui/$OWNER_UID/dev.snowline.$svc" || true
  fi
done
echo "done; check the four /health URLs (docs/ops/hub-daemons.md). The backup agent dev.snowline.backup was left as it is."
