#!/bin/sh
# Move the hub's four services from per-user LaunchAgents to system
# LaunchDaemons so they start at boot without a login (#239 / 239ec238),
# install the autocut timer (#258), and install the hourly DB backup agent
# (decision c68ec655) — which stays a per-user LaunchAgent on purpose, see
# docs/ops/hub-daemons.md "DB backups".
# Idempotent. Run:  sudo ops/hub/install-daemons.sh [--wait]
# Refuses (exit 75) while an autocut is running; --wait waits up to 15 min.
# Rollback:        sudo ops/hub/uninstall-daemons.sh
# See docs/ops/hub-daemons.md. Unit lists and helpers: launchdaemons/lib.sh.

[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

# shellcheck disable=SC1091
. "$(cd "$(dirname "$0")" && pwd)/launchdaemons/lib.sh"
autocut_guard "$@"

STAMP="$(date +%Y%m%d)"
START_EPOCH="$(date +%s)"   # the backup check below wants a dump newer than this install
fail=0

# --- the services: per-user agent -> system daemon, kickstarted ---
for svc in $SERVICES; do
  label="dev.snowline.$svc"
  echo "== $label"
  # 1. stop the agent (ignore: it may not be loaded) and wait for it to be
  #    gone — a service runs its whole startup before binding, so the daemon
  #    must never start while the agent is still shutting down ...
  launchctl bootout "gui/$OWNER_UID/$label" 2>/dev/null || true
  if ! wait_unloaded "gui/$OWNER_UID/$label"; then
    echo "   agent $label still registered after 30s; NOT starting the daemon on top of it — re-run the install" >&2
    fail=1
    continue
  fi
  # 2. ... and move its plist aside so it cannot reload at login and double-bind
  if [ -f "$AGENTS/$label.plist" ]; then
    mv "$AGENTS/$label.plist" "$AGENTS/$label.plist.disabled-$STAMP"
    echo "   agent plist -> $label.plist.disabled-$STAMP"
  fi
  # 3. install the daemon plist (replace a previously loaded daemon cleanly;
  #    wait for the old instance to be gone so the bootstrap cannot race it)
  launchctl bootout "system/$label" 2>/dev/null || true
  wait_unloaded "system/$label" || echo "   $label still registered after 30s; trying anyway" >&2
  cp "$LAUNCHDAEMONS_SRC/$label.plist" "$DAEMONS_DIR/$label.plist"
  chown root:wheel "$DAEMONS_DIR/$label.plist"
  chmod 644 "$DAEMONS_DIR/$label.plist"
  if launchctl bootstrap system "$DAEMONS_DIR/$label.plist"; then
    launchctl kickstart -k "system/$label"
  elif launchctl print "system/$label" >/dev/null 2>&1; then
    # the old registration never finished tearing down: its OLD definition
    # keeps running (a kickstart would only restart that); say so, re-run
    echo "   bootstrap refused and the old registration of $label is still loaded — re-run the install (or reboot)" >&2
    fail=1
  else
    echo "   bootstrap FAILED for $label (nothing loaded)" >&2
    fail=1
  fi
done

# --- system-only timers (autocut): bootstrapped, never kickstarted ---
# The launchd autocut timer is the ALTERNATIVE to the in-process autocut host
# (docs "Auto-cut"); when the host is enabled (~/.config/snowline/
# autocut.enabled) the timer is not installed, or two cutters would fire each
# hour. The timer is for a hub that runs the host disabled on purpose.
for timer in $TIMERS; do
  label="dev.snowline.$timer"
  echo "== $label (timer; not kickstarted)"
  if [ "$timer" = autocut ] && [ -e "$OWNER_HOME/.config/snowline/autocut.enabled" ]; then
    if launchctl print "system/$label" >/dev/null 2>&1; then
      echo "   in-process autocut host is enabled AND this timer is loaded: two cutters — remove one (docs 'Auto-cut')" >&2
      fail=1
    elif [ -f "$DAEMONS_DIR/$label.plist" ]; then
      # a stale plist would be loaded again at the next boot, next to the host
      rm -f "${DAEMONS_DIR:?}/${label:?}.plist"
      echo "   in-process autocut host is enabled; removed the stale (unloaded) timer plist $DAEMONS_DIR/$label.plist"
    else
      echo "   in-process autocut host is enabled; timer not installed"
    fi
    continue
  fi
  if cmp -s "$LAUNCHDAEMONS_SRC/$label.plist" "$DAEMONS_DIR/$label.plist" \
     && launchctl print "system/$label" >/dev/null 2>&1; then
    echo "   unchanged and loaded; left as it is"
    continue
  fi
  # autocut was refused up front by autocut_guard; a cut that started since
  # is a plain check here — still running means skip, never kill it
  if lock_held "$LOCKS_DIR/$timer.lock"; then
    echo "   $timer is running now; leaving $label as it is — re-run the install after it finishes" >&2
    fail=1
    continue
  fi
  launchctl bootout "system/$label" 2>/dev/null || true
  wait_unloaded "system/$label" || echo "   $label still registered after 30s; trying anyway" >&2
  cp "$LAUNCHDAEMONS_SRC/$label.plist" "$DAEMONS_DIR/$label.plist"
  chown root:wheel "$DAEMONS_DIR/$label.plist"
  chmod 644 "$DAEMONS_DIR/$label.plist"
  launchctl bootstrap system "$DAEMONS_DIR/$label.plist" || { echo "   bootstrap FAILED for $label" >&2; fail=1; }
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

# --- the backup agent: tracked plist -> ~/Library/LaunchAgents, loaded ---
# After the services are up: the agent's RunAtLoad pg_dump holds ACCESS
# SHARE on every table, which would block a service's boot migration.
# A per-user agent on purpose (docs "DB backups"): the dumps need the
# owner's session anyway (Postgres is the owner's agent, iCloud syncs per
# session), so a daemon would add no coverage, only a TCC problem. Operator
# overrides live in ~/.config/snowline/backup.env, never in the plist. The
# unit is (re)loaded — plist copied in at that point, never before — when
# the tracked plist differs, the unit is not loaded, its last run failed
# (exit 1 or 2; 3 = "stepped aside" is harmless), or no dump has landed in
# the last two hours (an exit code alone is not evidence: a unit launchd
# stopped firing still shows its old exit 0). A dump in progress (it holds
# backup.lock) is waited for first; the hourly fire landing in the instant
# between the last probe and the bootout would lose that hour's dump, which
# the RunAtLoad dump then replaces.
# Everything that looks at the destination is asked of the script itself,
# run as the owner (`--print-dest`, `--check <minutes>`): same backup.env,
# same TCC grant, same path — root never reads the owner's iCloud folder.
label="dev.snowline.backup"
log_file="$OWNER_HOME/Library/Logs/snowline-backup.log"
echo "== $label (per-user agent)"
as_owner() { sudo -u "#$OWNER_UID" -H /bin/bash "$OPS_HUB/snowline-backup.sh" "$@"; }
dest="$(as_owner --print-dest 2>/dev/null)" || dest="(unknown: --print-dest failed)"
# judge_dump <minutes> — prints OK / NONE / UNKNOWN for a dump newer than <minutes>
judge_dump() {
  as_owner --check "$1" >/dev/null 2>&1
  case $? in 0) echo OK ;; 1) echo NONE ;; *) echo UNKNOWN ;; esac
}
why=""
if ! cmp -s "$LAUNCHAGENTS_SRC/$label.plist" "$AGENTS/$label.plist" 2>/dev/null; then
  why="plist changed"
elif ! launchctl print "gui/$OWNER_UID/$label" >/dev/null 2>&1; then
  why="not loaded"
else
  code="$(unit_last_exit "gui/$OWNER_UID/$label")"
  case "$code" in
    ""|0|3) ;;
    *) why="last run exited $code" ;;
  esac
  if [ -z "$why" ]; then
    case "$(judge_dump 120)" in
      OK)   echo "   unchanged and loaded, dumped within 2 h; left as it is" ;;
      NONE) why="no dump in the last 2 h in $dest" ;;
      *)    echo "   unchanged and loaded; could not check $dest for a recent dump (as the owner) — check by hand: ls -t '$dest' | head" >&2; fail=1 ;;
    esac
  fi
fi
backup_loaded=no
if [ -n "$why" ]; then
  echo "   reloading ($why)"
  if ! unit_busy_wait backup 600; then
    echo "   a dump is still running; leaving $label as it is (plist NOT replaced) — re-run the install after it finishes" >&2
    fail=1
  else
    launchctl bootout "gui/$OWNER_UID/$label" 2>/dev/null || true
    if ! wait_unloaded "gui/$OWNER_UID/$label"; then
      # the plist on disk is left untouched, so the next run still sees
      # "plist changed" instead of mistaking the old registration for current
      echo "   old registration of $label still loaded after 30s; plist NOT replaced — re-run the install" >&2
      fail=1
    else
      cp "$LAUNCHAGENTS_SRC/$label.plist" "$AGENTS/$label.plist"
      chown "$OWNER_UID" "$AGENTS/$label.plist"
      chmod 644 "$AGENTS/$label.plist"
      if launchctl bootstrap "gui/$OWNER_UID" "$AGENTS/$label.plist"; then
        backup_loaded=yes
      else
        echo "   could NOT load $label into gui/$OWNER_UID (no login session?); it loads at the next login" >&2
        fail=1
      fi
    fi
  fi
fi

# --- the backup's RunAtLoad dump, if this install (re)loaded it ---
# The script may wait 120s for Postgres, then dump every store: allow 5 min.
# A fresh registration carries no exit code until its run ends, so the code
# read here is this run's; launchd may hold a freshly loaded job a moment
# before its first spawn, so the only early exit is the unit vanishing.
# Evidence is a dump newer than this install (asked of the script as the
# owner), not the exit code alone. Exit 3 ("stepped aside": a manual run
# took the lock first) is a retry, not a failure: wait for that run, then
# look for its dump.
if [ "$backup_loaded" = yes ]; then
  echo "== $label (RunAtLoad dump)"
  code=""; running=no; i=0
  while [ "$i" -lt 300 ]; do
    info="$(launchctl print "gui/$OWNER_UID/$label" 2>/dev/null)" || break   # unit gone
    if printf '%s\n' "$info" | grep -q -E '^[[:space:]]*(state = running|pid = [0-9])'; then
      running=yes
    else
      running=no
      code="$(printf '%s\n' "$info" | sed -n 's/.*last exit code = \([0-9][0-9]*\).*/\1/p' | head -n 1)"
      [ -n "$code" ] && break
    fi
    sleep 2
    i=$((i + 2))
  done
  tail -n 8 "$log_file" 2>/dev/null | sed 's/^/   /'
  if [ "$code" = 3 ]; then
    echo "   stepped aside (another run held the lock); waiting for that run instead"
    unit_busy_wait backup 600 || true
    code=0
  fi
  if [ "$code" = 0 ]; then
    mins=$(( ($(date +%s) - START_EPOCH) / 60 + 1 ))
    case "$(judge_dump "$mins")" in
      OK)   echo "   OK (dump landed in $dest since this install started)" ;;
      NONE) echo "   exit 0 but NO new dump in $dest — not trusting it; check $log_file" >&2; fail=1 ;;
      *)    echo "   exit 0, UNCONFIRMED: could not check $dest (as the owner) — verify by hand: ls -t '$dest' | head" >&2; fail=1 ;;
    esac
  elif [ -z "$code" ] && [ "$running" = yes ]; then
    echo "   still running after ${i}s — check $log_file and 'launchctl print gui/$OWNER_UID/$label'" >&2
    fail=1
  else
    echo "   FAILED (${code:+exit $code}${code:-never ran, or killed by a signal}) — see $log_file (1: a store failed; 2: cannot run here)" >&2
    fail=1
  fi
fi
exit "$fail"
