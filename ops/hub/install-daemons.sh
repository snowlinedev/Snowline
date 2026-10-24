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
autocut_guard "$@"

STAMP="$(date +%Y%m%d)"
# A file older than anything this install produces: the backup verifier
# requires a dump NEWER than it, not just an exit code.
START_MARK="$(mktemp -t snowline-install)"
trap 'rm -f "$START_MARK"' EXIT

# install_unit <name> <kick:yes|no> — the same steps for a service and a timer;
# only the trailing kickstart differs (a timer must never be kickstarted: that
# fires an immediate run). Sets UNIT_STATE for the caller:
#   installed   bootstrapped (and, for a service, kickstarted)
#   unchanged   timer: plist byte-identical and loaded, left alone
#   busy        timer: its run lock still held after the bound, left alone
#   still-old   bootstrap refused and the previous registration is still
#               loaded (old definition keeps running), left alone
#   not-loaded  bootstrap refused and nothing is loaded
# Returns 0 for installed/unchanged, 1 otherwise.
install_unit() {
  label="dev.snowline.$1"
  echo "== $label$([ "$2" = yes ] || echo ' (timer; not kickstarted)')"
  # A timer whose installed plist is byte-identical and which is loaded is
  # left alone: re-bootstrapping it would only fire a pointless RunAtLoad run
  # (and, for the backup, a 5-minute verdict wait). Services are always
  # cycled — this script is the deploy path and a restart picks up new code.
  if [ "$2" != yes ] && cmp -s "$LAUNCHDAEMONS_SRC/$label.plist" "$DAEMONS_DIR/$label.plist" \
     && launchctl print "system/$label" >/dev/null 2>&1; then
    echo "   unchanged and loaded; left as it is"
    UNIT_STATE=unchanged
    return 0
  fi
  # Never boot a unit out mid-run: the backup waits on backup.lock (a dump is
  # minutes at most); autocut was refused up front by autocut_guard (exit 75
  # or --wait), so a cut that started since is a plain check here. Still
  # running means skip this unit and say so, rather than killing the run.
  case "$1" in autocut) bound=0 ;; *) bound=600 ;; esac
  if ! unit_busy_wait "$1" "$bound"; then
    echo "   leaving $label as it is — re-run the install after it finishes" >&2
    UNIT_STATE=busy
    return 1
  fi
  # Units that had a per-user agent (the services, the agent-backed timers):
  # stop the agent (ignore: it may not be loaded) and move its plist aside so
  # it cannot reload at login and double-bind. A system-only timer never had
  # one, so any same-label agent plist is somebody's experiment: left alone.
  case " $SERVICES $AGENT_TIMERS " in
    *" $1 "*)
      launchctl bootout "gui/$OWNER_UID/$label" 2>/dev/null || true
      if [ -f "$AGENTS/$label.plist" ]; then
        mv "$AGENTS/$label.plist" "$AGENTS/$label.plist.disabled-$STAMP"
        echo "   agent plist -> $label.plist.disabled-$STAMP"
      fi
      ;;
  esac
  # Install the daemon plist, replacing a previously loaded daemon cleanly:
  # wait for the old instance to be gone so the bootstrap cannot race it.
  launchctl bootout "system/$label" 2>/dev/null || true
  wait_unloaded "system/$label" || echo "   $label still registered after 30s; trying anyway" >&2
  cp "$LAUNCHDAEMONS_SRC/$label.plist" "$DAEMONS_DIR/$label.plist"
  chown root:wheel "$DAEMONS_DIR/$label.plist"
  chmod 644 "$DAEMONS_DIR/$label.plist"
  if ! launchctl bootstrap system "$DAEMONS_DIR/$label.plist"; then
    # The old registration may not have finished tearing down: boot it out
    # again, wait again, retry once. (A kickstart would only restart the OLD
    # cached definition, so it is no substitute for a successful bootstrap.)
    launchctl bootout "system/$label" 2>/dev/null || true
    wait_unloaded "system/$label" || true
    if ! launchctl bootstrap system "$DAEMONS_DIR/$label.plist"; then
      if launchctl print "system/$label" >/dev/null 2>&1; then
        echo "   bootstrap FAILED for $label and its OLD registration is still loaded (old definition keeps running) — re-run, or reboot" >&2
        UNIT_STATE=still-old
      else
        echo "   bootstrap FAILED for $label (nothing loaded)" >&2
        UNIT_STATE=not-loaded
      fi
      return 1
    fi
  fi
  [ "$2" = yes ] && launchctl kickstart -k "system/$label"
  UNIT_STATE=installed
  return 0
}

fail=0
for svc in $SERVICES; do
  install_unit "$svc" yes || fail=1   # the /health poll below reports it too
done

# Timer outcomes, consulted by the verifiers below: "<name>=<state>" words.
timer_states=""
for timer in $SYSTEM_TIMERS $AGENT_TIMERS; do
  install_unit "$timer" no || fail=1
  timer_states="$timer_states $timer=$UNIT_STATE"
done
state_of() { for w in $timer_states; do case "$w" in "$1="*) echo "${w#*=}" ;; esac; done; }

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

# wait_run <domain/label> <log> — wait (up to 5 min) for a unit's current run
# to finish; prints the tail of its log; sets RUN_CODE ("" = never ran / was
# killed, or still running — RUN_RUNNING says which).
wait_run() {
  RUN_CODE=""; RUN_RUNNING=no; i=0
  while [ "$i" -lt 300 ]; do
    info="$(launchctl print "$1" 2>/dev/null)"
    RUN_CODE="$(printf '%s\n' "$info" | sed -n 's/.*last exit code = \([0-9][0-9]*\).*/\1/p' | head -n 1)"
    [ -n "$RUN_CODE" ] && break
    if printf '%s\n' "$info" | grep -q -E '^[[:space:]]*(state = running|pid = [0-9])'; then
      RUN_RUNNING=yes
    else
      RUN_RUNNING=no
      [ "$i" -ge 10 ] && break   # not running, no exit code: never spawned / signalled
    fi
    sleep 2
    i=$((i + 2))
  done
  tail -n 8 "$2" 2>/dev/null | sed 's/^/   /'
}

# fresh_dump <dest> — a *.dump (or its iCloud-evicted `.NAME.dump.icloud`
# placeholder) newer than START_MARK. Prints the name, or "?" when this
# (root, Terminal-spawned) shell is not allowed to read the owner's iCloud
# tree, or nothing.
fresh_dump() {
  err="$(mktemp -t snowline-install-find)"
  name="$(find "$1" -maxdepth 1 \( -name '*.dump' -o -name '.*.dump.icloud' \) -type f -newer "$START_MARK" 2>"$err" | head -n 1)"
  if [ -n "$name" ]; then basename "$name"; elif [ -s "$err" ]; then echo "?"; fi
  rm -f "$err"
}

# verify_backup — the backup unit proves itself on load. Only a run this
# install started (state installed) is judged; the script may wait 120s for
# Postgres first, then dump every store, so allow 5 minutes:
#   exit 0 + fresh dump   OK
#   exit 0, no fresh dump not trusted, fail (an exit code alone is not evidence)
#   exit 3                stepped aside (another run held the lock): fail, no rollback
#   exit 1                some store failed — identical under the agent: fail, no rollback
#   still running         fail, no rollback (never boot a healthy dump out)
#   exit 2 / never ran    the daemon cannot run here (TCC, destination):
#                         restore the per-user agent, then report THAT run too,
#                         because several exit-2 causes are identical under
#                         the agent (root-owned lock, missing lib.sh)
verify_backup() {
  label="dev.snowline.backup"
  log_file="$OWNER_HOME/Library/Logs/snowline-backup.log"
  echo "== $label (RunAtLoad run)"
  case "$(state_of backup)" in
    unchanged) echo "   not re-installed (unchanged); nothing to judge"; return ;;
    busy)      echo "   not re-installed (a dump was still running); re-run the install later" >&2; fail=1; return ;;
    still-old) echo "   not re-installed (old registration still loaded, keeps running); re-run the install later" >&2; fail=1; return ;;
    not-loaded)
      echo "   FAILED (daemon did not bootstrap, nothing loaded); removing its plist and restoring the per-user agent" >&2
      restore_agent backup || true
      fail=1; return ;;
  esac
  wait_run "system/$label" "$log_file"
  dest="$(plutil -extract EnvironmentVariables.SNOWLINE_BACKUP_DIR raw -o - "$DAEMONS_DIR/$label.plist" 2>/dev/null || echo "$BACKUP_DIR")"
  if [ "$RUN_CODE" = 0 ]; then
    fresh="$(fresh_dump "$dest")"
    case "$fresh" in
      "")  echo "   exit 0 but NO new dump in $dest — not trusting it; check $log_file" >&2; fail=1 ;;
      "?") echo "   exit 0; this shell may not read $dest to confirm a fresh dump — verify by hand: ls -t '$dest' | head" >&2 ;;
      *)   echo "   OK (exit 0, fresh dump $fresh)" ;;
    esac
  elif [ "$RUN_CODE" = 3 ]; then
    echo "   inconclusive (exit 3: another backup run held the lock); not rolling back — re-check in an hour" >&2; fail=1
  elif [ "$RUN_CODE" = 1 ]; then
    echo "   FAILED (exit 1: some store(s) failed; not rolling back — see $log_file)" >&2; fail=1
  elif [ -z "$RUN_CODE" ] && [ "$RUN_RUNNING" = yes ]; then
    echo "   still running after ${i}s; not rolling back — check $log_file and 'launchctl print system/$label'" >&2; fail=1
  else
    echo "   FAILED (${RUN_CODE:+exit $RUN_CODE}${RUN_CODE:-never ran, or killed by a signal}) — the daemon cannot run here; restoring the per-user agent" >&2
    fail=1
    if restore_agent backup keep-daemon-if-none; then
      echo "   agent restored; judging ITS RunAtLoad run (the cause may be the same under the agent):"
      wait_run "gui/$OWNER_UID/$label" "$log_file"
      case "$RUN_CODE" in
        0) echo "   agent: OK (exit 0) — backups continue under the per-user agent" ;;
        *) echo "   agent: ${RUN_CODE:+exit $RUN_CODE}${RUN_CODE:-no result} — backups are NOT running; fix the cause in $log_file now" >&2 ;;
      esac
    fi
  fi
}

for timer in $AGENT_TIMERS; do
  case "$timer" in
    backup) verify_backup ;;
    *) echo "== dev.snowline.$timer: no verifier for this unit; check it by hand" >&2; fail=1 ;;
  esac
done
exit "$fail"
