#!/bin/sh
# Move the hub's four services (and the hourly DB backup timer) from per-user
# LaunchAgents to system LaunchDaemons so they run at boot without a login
# (#239 / 239ec238; backup: decision c68ec655, item f304ec06).
# Idempotent. Run:  sudo ops/hub/install-daemons.sh [--wait]
# Refuses (exit 75) while an autocut is running; --wait waits up to 15 min.
# Rollback:        sudo ops/hub/uninstall-daemons.sh
# See docs/ops/hub-daemons.md. Unit lists and helpers: launchdaemons/lib.sh.

[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

# shellcheck disable=SC1091
. "$(cd "$(dirname "$0")" && pwd)/launchdaemons/lib.sh"
GUARD_ARG="${1:-}"
autocut_guard "$GUARD_ARG"

STAMP="$(date +%Y%m%d)"
# A file older than anything this install produces: the backup judge below
# requires a dump NEWER than it, not just an exit code.
START_MARK="$(mktemp -t snowline-install)"
trap 'rm -f "$START_MARK"' EXIT

# install_unit <name> <kick:yes|no> — the same steps for a service and a timer;
# only the trailing kickstart differs (a timer must never be kickstarted: that
# fires an immediate run). Returns non-zero if the bootstrap failed.
install_unit() {
  label="dev.snowline.$1"
  echo "== $label$([ "$2" = yes ] || echo ' (timer; not kickstarted)')"
  # 0. never boot a unit out mid-run: wait on the unit's own run lock
  #    (<name>.lock — autocut holds it for the cut, the backup for the dump;
  #    a service has none). Still busy after the bound: skip this unit and
  #    say so, rather than killing a run or refusing mid-install.
  if ! unit_busy_wait "$1"; then
    echo "   leaving $label as it is — re-run the install after it finishes" >&2
    return 1
  fi
  # 1./2. units that had a per-user agent (the services, the agent-backed
  #    timers): stop the agent (ignore: it may not be loaded) and move its
  #    plist aside so it cannot reload at login and double-bind. A
  #    system-only timer never had one, so any same-label agent plist is
  #    somebody's experiment and is left alone.
  case " $SERVICES $AGENT_TIMERS " in
    *" $1 "*)
      launchctl bootout "gui/$OWNER_UID/$label" 2>/dev/null || true
      if [ -f "$AGENTS/$label.plist" ]; then
        mv "$AGENTS/$label.plist" "$AGENTS/$label.plist.disabled-$STAMP"
        echo "   agent plist -> $label.plist.disabled-$STAMP"
      fi
      ;;
  esac
  # 3. install the daemon plist (replace a previously loaded daemon cleanly;
  #    wait for the old instance to be gone so the bootstrap cannot race it)
  launchctl bootout "system/$label" 2>/dev/null || true
  wait_unloaded "system/$label" || echo "   $label still registered after 30s; trying anyway" >&2
  cp "$LAUNCHDAEMONS_SRC/$label.plist" "$DAEMONS_DIR/$label.plist"
  chown root:wheel "$DAEMONS_DIR/$label.plist"
  chmod 644 "$DAEMONS_DIR/$label.plist"
  if ! launchctl bootstrap system "$DAEMONS_DIR/$label.plist"; then
    # A service whose old registration never finished tearing down is still
    # loaded: restart it onto the new plist rather than leave the old code
    # running behind an UP /health. Never for a timer (a kickstart fires a run).
    if [ "$2" = yes ] && launchctl print "system/$label" >/dev/null 2>&1; then
      echo "   bootstrap refused but $label is still loaded; restarting it" >&2
      launchctl kickstart -k "system/$label" && return 0
    fi
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

not_loaded=""
for timer in $SYSTEM_TIMERS $AGENT_TIMERS; do
  if ! install_unit "$timer" no; then
    fail=1
    not_loaded="$not_loaded $timer"
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

# Each AGENT_TIMERS unit proves itself on load: wait for its RunAtLoad run to
# finish — the backup script may wait 120s for Postgres first, then dump
# every store, so allow 5 minutes — and judge it:
#   exit 0            dumps landed — and a fresh *.dump in the destination
#                     proves it (an exit code alone is not evidence)
#   exit 3            stepped aside because another backup run held the lock:
#                     inconclusive, fail the install, do NOT roll back
#   exit 2            the daemon cannot run at all in the system domain (no
#                     destination / no iCloud root / cannot write — e.g. TCC,
#                     see docs/ops/hub-daemons.md "DB backups"): put the
#                     per-user agent back and fail the install
#   exit 1            some store(s) failed to dump — transient, identical
#                     under the agent: fail the install, do NOT roll back
#   still running     fail the install, do NOT roll back (a healthy in-flight
#                     dump must not be booted out)
#   never ran / killed (no pid, no exit code): treated like exit 2
#   did not bootstrap the daemon plist is removed and the agent put back
#                     (nothing was loaded, so there is no daemon to keep)
for timer in $AGENT_TIMERS; do
  label="dev.snowline.$timer"
  echo "== $label (RunAtLoad run)"
  case " $not_loaded " in
    *" $timer "*)
      echo "   $timer: FAILED (daemon did not bootstrap); removing its plist and restoring the per-user agent" >&2
      restore_agent "$timer" || true
      fail=1
      continue
      ;;
  esac
  code=""; running=no; i=0
  while [ "$i" -lt 300 ]; do
    info="$(launchctl print "system/$label" 2>/dev/null)"
    code="$(printf '%s\n' "$info" | sed -n 's/.*last exit code = \([0-9][0-9]*\).*/\1/p' | head -n 1)"
    [ -n "$code" ] && break
    if printf '%s\n' "$info" | grep -q -E '^[[:space:]]*(state = running|pid = [0-9])'; then
      running=yes
    else
      running=no
      # not running and no exit code: it never spawned or died by signal
      [ "$i" -ge 10 ] && break
    fi
    sleep 2
    i=$((i + 2))
  done
  tail -n 8 "$OWNER_HOME/Library/Logs/snowline-$timer.log" 2>/dev/null | sed 's/^/   /'
  if [ "$code" = 0 ]; then
    dest="$(plutil -extract EnvironmentVariables.SNOWLINE_BACKUP_DIR raw -o - "$DAEMONS_DIR/$label.plist" 2>/dev/null || echo "$BACKUP_DIR")"
    if find "$dest" -maxdepth 1 -name '*.dump' -type f -newer "$START_MARK" 2>/dev/null | grep -q .; then
      echo "   $timer: OK (exit 0, fresh dump in $dest)"
    else
      echo "   $timer: exit 0 but NO new dump in $dest — not trusting it; check $OWNER_HOME/Library/Logs/snowline-$timer.log" >&2
      fail=1
    fi
  elif [ "$code" = 3 ]; then
    echo "   $timer: inconclusive (exit 3: another backup run held the lock; nothing dumped by this unit yet); not rolling back — re-check in an hour" >&2
    fail=1
  elif [ "$code" = 1 ]; then
    echo "   $timer: FAILED (exit 1: some store(s) failed; not rolling back — see $OWNER_HOME/Library/Logs/snowline-$timer.log)" >&2
    fail=1
  elif [ -z "$code" ] && [ "$running" = yes ]; then
    echo "   $timer: still running after ${i}s; not rolling back — check $OWNER_HOME/Library/Logs/snowline-$timer.log and 'launchctl print system/$label'" >&2
    fail=1
  else
    echo "   $timer: FAILED (${code:+exit $code}${code:-never ran, or killed by a signal}) — the daemon cannot run here; restoring the per-user agent" >&2
    restore_agent "$timer" keep-daemon-if-none || true
    fail=1
  fi
done
exit "$fail"
