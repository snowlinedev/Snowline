#!/bin/sh
# Install, (re)load and prove the hub's hourly DB backup agent,
# dev.snowline.backup (decision c68ec655) — the owner's per-user
# LaunchAgent, tracked at ops/hub/launchagents/dev.snowline.backup.plist.
# Run AS THE OWNER, no sudo:   ops/hub/install-backup-agent.sh
# (install-daemons.sh runs it for you, as the owner, at the end.)
# Idempotent and cheap to re-run: a loaded, unchanged, recently-dumping agent
# is left alone. See docs/ops/hub-daemons.md "DB backups".
#
# Exit: 0 = agent loaded and (if it was (re)loaded) proven by one run with
# evidence; 1 = something needs the operator (message says what).

if [ "$(id -u)" = 0 ]; then
  echo "run this as the hub owner, not root: the unit is a per-user agent" >&2
  exit 64
fi

# shellcheck disable=SC1091
. "$(cd "$(dirname "$0")" && pwd)/launchdaemons/lib.sh"
BACKUP_SCRIPT="$(backup_script)" || BACKUP_SCRIPT=""

label="dev.snowline.backup"
domain="gui/$(id -u)"
plist_src="$LAUNCHAGENTS_SRC/$label.plist"
plist_dst="$AGENTS/$label.plist"
log_file="$BACKUP_LOG"
fail=0

# --- guards: the unit runs the canonical script; this install must BE it ---
# The plist carries the canonical checkout path on purpose (a path taken
# from whichever checkout runs this would make a scratch worktree the
# permanent unit and die with EX_CONFIG when it is deleted). So: that file
# must exist, and this install must run from that very checkout (same
# file, not merely equal bytes — it sources the canonical lib.sh and
# backup.env too).
if [ -z "$BACKUP_SCRIPT" ]; then
  echo "cannot read the script path from $plist_src (plutil? malformed plist?)" >&2
  exit 1
fi
if [ ! -f "$BACKUP_SCRIPT" ]; then
  echo "$BACKUP_SCRIPT is not on this hub (checkout moved? then update the path in the tracked plist); NOT installing a unit that would fail with EX_CONFIG" >&2
  exit 1
fi
if [ ! "$OPS_HUB/snowline-backup.sh" -ef "$BACKUP_SCRIPT" ]; then
  echo "this install runs from $OPS_HUB, but the unit runs $BACKUP_SCRIPT; run it from the canonical checkout (after pulling)" >&2
  exit 1
fi
echo "== $label (per-user agent; unit runs $BACKUP_SCRIPT)"
[ -e "$log_file" ] || touch "$log_file"

# --- evidence helpers: ask the script (same backup.env, same destination) ---
# With this shell's SNOWLINE_BACKUP_* unset: the agent under launchd sees
# only backup.env, and the probes must see the same, or a leftover export
# from a scratch test would make them judge a different folder.
query() { env -u SNOWLINE_BACKUP_DIR -u SNOWLINE_BACKUP_RETENTION_DAYS -u SNOWLINE_BACKUP_DBS /bin/bash "$BACKUP_SCRIPT" "$@"; }
dest="$(query --print-dest 2>/dev/null)" || dest="(unknown: --print-dest failed)"
# dump_evidence <minutes> — sets EVIDENCE to OK (a dump newer than <minutes>
# in the destination), NONE (readable, none there), LOG_OK / LOG_FAIL (the
# destination cannot be read from this context — Terminal or ssh without
# Full Disk Access — so LOGSAYS, from a log_verdict the caller ran first,
# stands in), or UNKNOWN; EVIDENCE_MSG carries the --check reason.
dump_evidence() {
  EVIDENCE_MSG="$(query --check "$1" 2>&1)"; de_rc=$?
  EVIDENCE_MSG="$(printf '%s\n' "$EVIDENCE_MSG" | tail -n 1)"
  case $de_rc in
    0) EVIDENCE=OK; return ;;
    1) EVIDENCE=NONE; return ;;
  esac
  # destination not readable from here: the caller's log_verdict (computed
  # once, before this) stands in
  case "$LOGSAYS" in
    OK)   EVIDENCE=LOG_OK ;;
    FAIL) EVIDENCE=LOG_FAIL ;;
    *)    EVIDENCE=UNKNOWN ;;
  esac
}
# log_verdict <since-stamp> — sets LOGSAYS to OK / FAIL / NONE from the
# agent's own log since <since-stamp>: the script's EXIT trap writes one
# machine-readable "run exit <code>" line per run, whatever the path. The
# LAST such line is the verdict (0 = OK; 3, a step-aside, is not a verdict
# at all and the line before it is consulted; anything else = FAIL), so an
# old failure cannot outrank a later clean run and a run cut short by
# SIGKILL (no line) is NONE, not OK.
log_verdict() {
  LOGSAYS=NONE
  for lv_code in $(awk -v s="$1" '$1 ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T/ && $1 >= s && $2 == "run" && $3 == "exit" { print $4 }' "$log_file" 2>/dev/null | tail -r); do
    case "$lv_code" in
      3) continue ;;
      0) LOGSAYS=OK; return ;;
      *) LOGSAYS=FAIL; return ;;
    esac
  done
}

# --- decide: leave alone, or (re)load ---
# (Re)load — plist copied in at that point, never before — when the tracked
# plist differs from the one launchd last LOADED (recorded by checksum in a
# marker when a load succeeded: the file on disk alone cannot tell, since a
# load can fail with the old registration still running), the unit is not
# loaded, its last run exited with anything but 0 or 3 (3 = "stepped aside"
# is harmless), or no evidence that THE UNIT dumped in the last two hours:
# a fresh dump in the destination AND a "run exit" line in the agent's log
# (a manual run writes to the terminal, not the log, so a dump alone could
# be the operator's, masking a unit launchd stopped firing; an exit code
# alone is no evidence either — a stopped unit still shows its old 0).
LOADED_MARK="$LOCKS_DIR/backup-agent.loaded"
plist_sum="$(cksum < "$plist_src" | cut -d' ' -f1)"
why=""
if ! cmp -s "$plist_src" "$plist_dst" 2>/dev/null; then
  why="plist changed"
elif [ "$(cat "$LOADED_MARK" 2>/dev/null)" != "$plist_sum" ]; then
  why="this plist was never confirmed loaded (a previous load did not complete?)"
elif ! unit_snapshot "$domain/$label"; then
  why="not loaded"
else
  case "$UNIT_EXIT" in
    ""|0|3) ;;
    2)
      # "cannot run here" (destination or volume missing, TCC, bad
      # configuration): a reload changes nothing; report, do not churn
      echo "   loaded, but its last run exited 2 (cannot run here) — fix the cause in $log_file; a reload would not help" >&2
      fail=1 ;;
    *) why="last run exited $UNIT_EXIT" ;;
  esac
  if [ -z "$why" ] && [ "$fail" = 0 ]; then
    since="$(date -u -v-120M +%FT%TZ)"
    log_verdict "$since"   # the unit's own account; a manual run leaves no line here
    dump_evidence 120
    case "$EVIDENCE:$LOGSAYS" in
      OK:OK|LOG_OK:OK) echo "   unchanged and loaded, the unit dumped within 2 h; left as it is" ;;
      OK:NONE)         why="a dump landed within 2 h but the unit's log shows no run (a manual dump? the unit not firing?)" ;;
      NONE:*)          why="no dump in the last 2 h in $dest" ;;
      *:FAIL|LOG_FAIL:*) why="the unit's last run within 2 h failed" ;;
      *)               why="no evidence the unit ran in the last 2 h (destination: $EVIDENCE_MSG)" ;;
    esac
  fi
fi
loaded=no
if [ -n "$why" ]; then
  echo "   reloading ($why)"
  # never boot the unit out mid-dump (it holds backup.lock); the hourly fire
  # landing in the instant between the last probe and the bootout would
  # lose that hour's dump, which the proving run then replaces
  if ! unit_busy_wait backup 600; then
    echo "   a dump is still running (or its lock cannot be probed); leaving $label as it is — re-run later" >&2
    fail=1
  elif place_unit "$plist_src" "$plist_dst" "$(id -u)" && load_unit "$label" "$domain" "$plist_dst"; then
    loaded=yes
    mkdir -p "$LOCKS_DIR" && printf '%s\n' "$plist_sum" > "$LOADED_MARK"
  else
    rm -f "$LOADED_MARK"   # whatever launchd runs now, it is not confirmed to be this plist
    fail=1
  fi
fi

# --- prove a (re)loaded unit with one run through launchd ---
# `kickstart`, so the run happens in the agent's OWN launchd session (its
# TCC posture, its environment), not this shell's. Completion is seen in
# launchd's run counter and the exit code is the agent's; evidence is a
# dump newer than the run (or the agent's own log lines when the
# destination cannot be read from here). Exit 3 (an hourly fire held the
# lock) is waited out and judged by the same evidence, with the window
# reaching back to the run that held the lock.
if [ "$loaded" = yes ]; then
  echo "== $label (proving run, via launchd)"
  unit_snapshot "$domain/$label"; runs_before="$UNIT_RUNS"
  run_stamp="$(date -u +%FT%TZ)"; run_epoch="$(date +%s)"
  if ! launchctl kickstart "$domain/$label"; then
    echo "   kickstart refused; the unit is loaded but unproven — run 'bash $BACKUP_SCRIPT' by hand" >&2
    fail=1
  else
    code=""; i=0; spawned=no
    while [ "$i" -lt 900 ]; do   # 120 s Postgres wait + up to 60 s lock wait per store + the dumps
      unit_snapshot "$domain/$label" || break   # unit gone
      [ "$UNIT_RUNNING" = yes ] && spawned=yes
      if [ "$UNIT_RUNNING" = no ] && [ "$UNIT_RUNS" -gt "$runs_before" ]; then
        code="$UNIT_EXIT"; break
      fi
      # a kickstart that launchd accepted but could not spawn (EX_CONFIG:
      # the program vanished) never increments runs: give up after a minute
      if [ "$spawned" = no ] && [ "$i" -ge 60 ]; then break; fi
      sleep 2
      i=$((i + 2))
    done
    deferred=no
    if [ "$code" = 3 ]; then
      # deferring to a concurrent run: wait for it to finish (if it does
      # not within the bound, say so and stop — nothing was proven), then
      # its OWN "run exit" line, which necessarily postdates this
      # kickstart, must be clean before its dumps count; its dump stamps
      # predate the kickstart by its runtime, so that window reaches back
      echo "   stepped aside (another run held the lock); deferring to that run"
      if ! unit_busy_wait backup 600; then
        echo "   the concurrent run is still going after 10 min; nothing proven — re-run later" >&2
        fail=1; code=""; skip_verdict=yes
      else
        deferred=yes; code=0
        log_verdict "$run_stamp"
        [ "$LOGSAYS" = FAIL ] && code=1
        run_epoch=$((run_epoch - 1800))
      fi
    fi
    tail -n 8 "$log_file" 2>/dev/null | sed 's/^/   /'
    if [ "$deferred" = yes ]; then how="the concurrent run completed"; else how="exit $code"; fi
    if [ "${skip_verdict:-no}" = yes ]; then
      :
    elif [ "$code" = 0 ]; then
      [ "$deferred" = yes ] || log_verdict "$run_stamp"
      dump_evidence $(( ($(date +%s) - run_epoch) / 60 + 1 ))
      case "$EVIDENCE" in
        OK)       echo "   OK ($how; dump landed in $dest)" ;;
        LOG_OK)   echo "   OK ($how; the log shows the dump — destination not checkable from this context: $EVIDENCE_MSG)" ;;
        NONE)     echo "   exit 0 but NO new dump in $dest — not trusting it; check $log_file" >&2; fail=1 ;;
        LOG_FAIL) echo "   exit 0 but the log's last run failed — check $log_file" >&2; fail=1 ;;
        *)        echo "   exit 0, UNCONFIRMED: could not check $dest ($EVIDENCE_MSG) and the log shows no dump — verify by hand: ls -t '$dest' | head" >&2; fail=1 ;;
      esac
    elif [ -z "$code" ]; then
      echo "   no result after ${i}s (still running, never spawned, or killed by a signal) — check $log_file and 'launchctl print $domain/$label'" >&2
      fail=1
    else
      [ "$deferred" = yes ] && how="the concurrent run's last line reports a failure"
      echo "   FAILED ($how) — see $log_file (1: a store failed; 2: cannot run here)" >&2
      fail=1
    fi
  fi
fi
exit "$fail"
