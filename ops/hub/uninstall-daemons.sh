#!/bin/sh
# Reverse ops/hub/install-daemons.sh: remove the system LaunchDaemons and put
# the per-user LaunchAgents back (#239). Run:  sudo ops/hub/uninstall-daemons.sh [--wait]
# Refuses (exit 75) while an autocut is running; --wait waits up to 15 min.
# The hourly DB backup agent is not part of the daemon move and is left as
# it is. Unit lists and helpers: launchdaemons/lib.sh.

# (the argument check comes before the sudo re-exec: a typo must not cost a password prompt)
case "${1:-}" in --wait|"") ;; *) echo "unknown argument '$1' (only --wait is accepted)" >&2; exit 64 ;; esac
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

unset CDPATH   # before the `cd` below: a caller's CDPATH would make it print and wander
# shellcheck disable=SC1091
. "$(cd "$(dirname "$0")" && pwd)/launchdaemons/lib.sh"
autocut_guard "$@"
fail=0

# System-only timers: no LaunchAgent predecessor; just remove (skipped, with
# a message, if a run started since the guard above).
for timer in $TIMERS; do
  label="dev.snowline.$timer"
  echo "== $label"
  if timer_skip "$timer" "the uninstall"; then fail=1; continue; fi
  launchctl bootout "system/$label" 2>/dev/null || true
  rm -f "${DAEMONS_DIR:?}/${label:?}.plist"
done

# Services: restore_agent (lib.sh) removes the daemon, puts the newest
# .disabled-* agent plist back (or keeps a live one) and loads it (the
# agents are RunAtLoad + KeepAlive: loading starts them).
for svc in $SERVICES; do
  echo "== dev.snowline.$svc"
  restore_agent "$svc" || fail=1
done
echo "done; check the four /health URLs (docs/ops/hub-daemons.md). The backup agent dev.snowline.backup was left as it is."
exit "$fail"
