#!/bin/sh
# Move the hub's four services from per-user LaunchAgents to system
# LaunchDaemons so they start at boot without a login (#239 / 239ec238),
# install the autocut launchd timer (#258) when the in-process host is not
# enabled, and install the hourly DB backup agent (decision c68ec655) —
# which stays a per-user LaunchAgent on purpose, see docs/ops/hub-daemons.md
# "DB backups".
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
  # stop the agent (ignore: it may not be loaded) and wait for it to be
  # gone — a service runs its whole startup before binding, so the daemon
  # must never start while the agent is still shutting down ...
  launchctl bootout "gui/$OWNER_UID/$label" 2>/dev/null || true
  if ! wait_unloaded "gui/$OWNER_UID/$label"; then
    echo "   agent $label still registered after 30s; NOT starting the daemon on top of it — re-run the install" >&2
    fail=1
    continue
  fi
  # ... and move its plist aside so it cannot reload at login and double-bind
  if [ -f "$AGENTS/$label.plist" ]; then
    mv "$AGENTS/$label.plist" "$AGENTS/$label.plist.disabled-$STAMP"
    echo "   agent plist -> $label.plist.disabled-$STAMP"
  fi
  load_unit "$label" system "$LAUNCHDAEMONS_SRC/$label.plist" "$DAEMONS_DIR/$label.plist" root:wheel yes || fail=1
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
    echo "   $timer is running now (or its lock cannot be probed); leaving $label as it is — re-run the install after it finishes" >&2
    fail=1
    continue
  fi
  load_unit "$label" system "$LAUNCHDAEMONS_SRC/$label.plist" "$DAEMONS_DIR/$label.plist" root:wheel no || fail=1
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
# After the services are up, so its RunAtLoad dump does not land in a boot
# migration's window (the dump itself also bounds that with pg_dump
# --lock-wait-timeout). A per-user agent on purpose (docs "DB backups"):
# the dumps need the owner's session anyway (Postgres is the owner's agent,
# iCloud syncs per session), so a daemon would add no coverage, only a TCC
# problem. Operator overrides live in ~/.config/snowline/backup.env, never
# in the plist. The plist carries the canonical checkout path for the script
# (never a path taken from whichever checkout runs this install), and is not
# loaded at all when nothing is at that path. The unit is (re)loaded — plist
# copied in at that point, never before — when the tracked plist differs
# from the installed one, the unit is not
# loaded, its last run exited with anything but 0 or 3 (3 = "stepped
# aside" is harmless), or no dump has landed in the last two hours (an exit
# code alone is not evidence: a unit launchd stopped firing still shows its
# old exit 0). A dump in progress (it holds backup.lock) is waited for
# first; the hourly fire landing in the instant between the last probe and
# the bootout would lose that hour's dump, which the RunAtLoad dump then
# replaces.
# Everything that looks at the destination is asked of the script itself,
# run as the owner (`--print-dest`, `--check <minutes>`): same backup.env,
# same TCC grant, same path — root never reads the owner's iCloud folder.
label="dev.snowline.backup"
log_file="$OWNER_HOME/Library/Logs/snowline-backup.log"
echo "== $label (per-user agent)"
plist_src="$LAUNCHAGENTS_SRC/$label.plist"
script="$(backup_agent_script)"
if [ -z "$script" ] || [ ! -f "$script" ]; then
  echo "   the tracked agent plist runs '$script', which is not on this hub (checkout moved? then edit ops/hub/launchagents/$label.plist); NOT installing a unit that would fail with EX_CONFIG" >&2
  fail=1
  exit "$fail"
fi
# The unit runs the CANONICAL script, which sources the canonical lib.sh and
# backup.env. This install must therefore BE the canonical checkout (same
# file, not merely equal bytes): from a branch or worktree it would install
# a unit running other code and send the queries below to a script that may
# not know them. Refuse otherwise.
if [ ! "$OPS_HUB/snowline-backup.sh" -ef "$script" ]; then
  echo "   this install runs from $OPS_HUB, but the unit runs $script; run the install from the canonical checkout (after pulling) — NOT touching the backup unit" >&2
  fail=1
  exit "$fail"
fi
echo "   unit runs $script"
as_owner() { sudo -u "#$OWNER_UID" -H /bin/bash "$script" "$@"; }
dest="$(as_owner --print-dest 2>/dev/null)" || dest="(unknown: --print-dest failed)"
# judge_dump <minutes> — OK / NONE / UNKNOWN for a dump newer than <minutes>;
# the token comes from the script's stdout, so a sudo or script failure
# (nothing printed) is UNKNOWN, never mistaken for "no dump"
judge_dump() {
  jd="$(as_owner --check "$1" 2>/dev/null)"
  case "$jd" in OK) echo OK ;; NONE) echo NONE ;; *) echo UNKNOWN ;; esac
}
why=""
if ! cmp -s "$plist_src" "$AGENTS/$label.plist" 2>/dev/null; then
  why="plist changed"
elif ! launchctl print "gui/$OWNER_UID/$label" >/dev/null 2>&1; then
  why="not loaded"
else
  unit_snapshot "gui/$OWNER_UID/$label"
  case "$UNIT_EXIT" in
    ""|0|3) ;;
    *) why="last run exited $UNIT_EXIT" ;;
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
    echo "   a dump is still running (or its lock cannot be probed); leaving $label as it is — re-run the install later" >&2
    fail=1
  elif load_unit "$label" "gui/$OWNER_UID" "$plist_src" "$AGENTS/$label.plist" "$OWNER_UID" no; then
    backup_loaded=yes
  else
    fail=1
  fi
fi

# --- the backup's RunAtLoad dump, if this install (re)loaded it ---
# The script may wait 120s for Postgres, then dump every store: allow 5 min.
# A fresh registration carries no exit code until its run ends, so the code
# read here is this run's; launchd may hold a freshly loaded job a moment
# before its first spawn, so the only early exit is the unit vanishing.
# Evidence is a dump newer than this install (asked of the script as the
# owner), not the exit code alone. Exit 3 ("stepped aside": a manual run
# took the lock first) is retried once: wait for that run, kickstart the
# unit, and judge THAT run by its own exit code and evidence.
# judge_run <runs-before> — wait for a run that launchd has not necessarily
# spawned yet: an exit code is only accepted once `runs` has advanced past
# <runs-before> (or the unit was seen running), so a stale code from the
# previous run is never taken for this one.
judge_run() {
  jr_before="$1"; code=""; running=no; seen=no; i=0
  while [ "$i" -lt 300 ]; do
    unit_snapshot "gui/$OWNER_UID/$label" || break   # unit gone
    running="$UNIT_RUNNING"
    [ "$running" = yes ] && seen=yes
    if [ "$running" = no ] && { [ "$seen" = yes ] || [ "$UNIT_RUNS" -gt "$jr_before" ]; }; then
      code="$UNIT_EXIT"   # may be empty: the run died by a signal; judged below
      break
    fi
    sleep 2
    i=$((i + 2))
  done
  tail -n 8 "$log_file" 2>/dev/null | sed 's/^/   /'
}
if [ "$backup_loaded" = yes ]; then
  echo "== $label (RunAtLoad dump)"
  judge_run 0   # a fresh registration starts at runs = 0
  if [ "$code" = 3 ]; then
    echo "   stepped aside (another run held the lock); waiting for that run, then firing the unit once more"
    unit_busy_wait backup 600 || true
    unit_snapshot "gui/$OWNER_UID/$label"; runs_before="$UNIT_RUNS"
    run_mark=$(date +%s)
    launchctl kickstart "gui/$OWNER_UID/$label"
    judge_run "$runs_before"
    START_EPOCH=$run_mark
  fi
  if [ "$code" = 0 ]; then
    mins=$(( ($(date +%s) - START_EPOCH) / 60 + 1 ))
    case "$(judge_dump "$mins")" in
      OK)   echo "   OK (exit 0; dump landed in $dest since this install started)" ;;
      NONE) echo "   exit 0 but NO new dump in $dest — not trusting it; check $log_file" >&2; fail=1 ;;
      *)    echo "   exit 0, UNCONFIRMED: could not check $dest (as the owner) — verify by hand: ls -t '$dest' | head" >&2; fail=1 ;;
    esac
  elif [ -z "$code" ] && [ "$running" = yes ]; then
    echo "   still running after ${i}s — check $log_file and 'launchctl print gui/$OWNER_UID/$label'" >&2
    fail=1
  else
    echo "   FAILED (${code:+exit $code}${code:-never ran, or killed by a signal}) — see $log_file (1: a store failed; 2: cannot run here; 3: lock held twice in a row)" >&2
    fail=1
  fi
fi
exit "$fail"
