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
# --deploy (what install-daemons.sh passes): called from a service rollout,
# so never wait long on the backup — a dump in progress is skipped (re-run
# later) and the proof is bounded to 10 minutes instead of 15.

# shellcheck disable=SC1091
. "$(cd "$(dirname "$0")" && pwd)/launchdaemons/lib.sh"
if [ "$(id -u)" != "$OWNER_UID" ]; then
  echo "run this as the hub owner (uid $OWNER_UID), not root or another account: the unit is the owner's per-user agent and every path here is the owner's" >&2
  exit 64
fi
BACKUP_SCRIPT="$(backup_script)" || BACKUP_SCRIPT=""

case "${1:-}" in
  --deploy) DEPLOY=yes ;;
  "") DEPLOY=no ;;
  *) echo "unknown argument '$1' (only --deploy is accepted)" >&2; exit 64 ;;
esac
# (a run is 120 s of Postgres wait at most, up to 60 s of lock wait per
# store, then the dumps: 15 min covers that; the 10 min of --deploy does
# not quite when EVERY store is lock-contended, and then its verdict is
# "still running", not a failure of the unit)
if [ "$DEPLOY" = yes ]; then BUSY_BOUND=0; PROVE_BOUND=600; DEPLOY_NOTE=" (a --deploy call does not wait longer)"; else BUSY_BOUND=600; PROVE_BOUND=900; DEPLOY_NOTE=""; fi

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
# in the destination), NONE (readable, none there), CONFIG_FAIL (the script
# refused its own configuration: it cannot run at all now), LOG_OK /
# LOG_FAIL (the destination cannot be read from this context — Terminal or
# ssh without Full Disk Access — so LOGSAYS, from a log_verdict the caller
# ran first, stands in), or UNKNOWN; EVIDENCE_MSG carries the --check reason.
dump_evidence() {
  de_err="$(mktemp -t snowline-install-check)" || { EVIDENCE=UNKNOWN; EVIDENCE_MSG="cannot create a temp file (TMPDIR unwritable?)"; return; }
  de_out="$(query --check "$1" 2>"$de_err")"; de_rc=$?
  # (the last stderr line, without the script's own stamp)
  EVIDENCE_MSG="$(tail -n 1 "$de_err" | strip_stamp)"; rm -f "$de_err"
  case $de_rc in
    0) EVIDENCE=OK; return ;;
    1) EVIDENCE=NONE; return ;;
    2) ;;
    *) EVIDENCE=UNKNOWN; EVIDENCE_MSG="--check could not run (exit $de_rc${EVIDENCE_MSG:+: $EVIDENCE_MSG})"; return ;;
  esac
  # exit 2 is either "destination unreadable from here" (the script says
  # UNREADABLE on stdout) or the script REFUSING its configuration (a die
  # on stderr, nothing on stdout) — the latter is a failure of the unit
  # itself, never a reason to fall back to old log evidence
  if [ "$de_out" = MISSING ]; then
    EVIDENCE=CONFIG_FAIL; EVIDENCE_MSG="destination not usable: ${EVIDENCE_MSG:-$dest is missing} — every run exits 2"
    return
  fi
  if [ "$de_out" != UNREADABLE ]; then
    EVIDENCE=CONFIG_FAIL
    return
  fi
  # destination not readable from here: the caller's log_verdict (computed
  # once, before this) stands in
  case "$LOGSAYS" in
    OK)   EVIDENCE=LOG_OK ;;
    FAIL) EVIDENCE=LOG_FAIL ;;
    *)    EVIDENCE=UNKNOWN ;;
  esac
}
# log_verdict <since-stamp> — sets LOGSAYS to OK / FAIL / NONE / OPEN from the
# agent's own log since <since-stamp>: the script's EXIT trap writes one
# machine-readable "run exit <code>" line per run, whatever the path. The
# LAST such line is the verdict (0 = OK; 3, a step-aside, is not a verdict
# at all and the line before it is consulted; anything else = FAIL), so an
# old failure cannot outrank a later clean run and a run cut short by
# SIGKILL (no line) is NONE, not OK — or OPEN when its "run start" is the
# last marker (never ended: in flight, or killed).
# log_lines_since <stamp> — the agent's own stamped log lines at or after
# <stamp> (pg_dump's unstamped stderr shares the file and is dropped): the
# ONE reader of the log format, for log_verdict and the in-flight start.
log_lines_since() {
  awk -v s="$1" -v re="$LOG_STAMP_RE" '$1 ~ re && $1 >= s' "$log_file" 2>/dev/null
}
# strip_stamp — a stamped line without its stamp (lib.sh's grammar)
strip_stamp() { sed -E "s/${LOG_STAMP_RE%\$} //"; }
log_verdict() {
  LOGSAYS=NONE
  # a "run start" with no "run exit" after it is a run that never ended:
  # in flight now, or killed (SIGKILL, launchd's exit timeout) — launchctl's
  # exit-code rendering is not relied on for that
  if [ "$(log_lines_since "$1" | awk '$2 == "run" && ($3 == "start" || $3 == "exit") { l = $3 } END { print l }')" = start ]; then
    LOGSAYS=OPEN; return
  fi
  for lv_code in $(log_lines_since "$1" | awk '$2 == "run" && $3 == "exit" { print $4 }' | tail -r); do
    case "$lv_code" in
      3) continue ;;
      0) LOGSAYS=OK; return ;;
      *) LOGSAYS=FAIL; return ;;
    esac
  done
}

# --- decide: leave alone, reload, or re-prove ---
# RELOAD (plist copied in at that point, never before) only for what a
# reload can change: the tracked plist differs from the one launchd last
# confirmed loaded (unit_current — bytes on disk alone cannot tell, since a
# load can fail with the old registration still running), or the unit is
# not loaded. RE-PROVE (fire one run, judge it) when the unit is loaded and
# current but there is no evidence it is healthy: its last run exited with
# anything but 0 or 3, or nothing shows THE UNIT dumped in the last two
# hours — a fresh dump in the destination AND a "run exit" line in the
# agent's log (a manual run writes to the terminal, not the log, so a dump
# alone could be the operator's, masking a unit launchd stopped firing; an
# exit code alone is no evidence either — a stopped unit still shows its
# old 0). Otherwise LEAVE ALONE.
action=leave; why=""
# (the snapshot can only fail in the instant after unit_unchanged saw the
# unit loaded; its values are needed below)
if ! unit_unchanged "$label" "$domain" "$plist_src" "$plist_dst" || ! unit_snapshot "$domain/$label"; then
  action=reload; why="${UNIT_UNCHANGED_WHY:-not loaded}"
else
  # an empty exit code with runs > 0 and nothing running is a run that
  # ended by a signal (launchd's "never exited"): not fine. (A first run
  # still in flight shows the same empty code; that is judged as in-flight.)
  [ "$UNIT_RUNNING" != yes ] && [ "$UNIT_RUNS" -gt 0 ] && [ -z "$UNIT_EXIT" ] && UNIT_EXIT=signal
  case "$UNIT_EXIT" in
    ""|0|3) ;;
    signal) action=reprove; why="last run ended by a signal (killed mid-dump?)" ;;
    78) action=reload; why="launchd could not spawn the program (EX_CONFIG) — a reload clears the penalty box" ;;
    *) action=reprove; why="last run exited $UNIT_EXIT" ;;
  esac
  if [ "$action" = leave ]; then
    since="$(date -u -v-120M +%FT%TZ)"
    log_verdict "$since"   # the unit's own account; a manual run leaves no line here
    dump_evidence 120
    case "$EVIDENCE:$LOGSAYS" in
      CONFIG_FAIL:*)   echo "   the script refuses its configuration ($EVIDENCE_MSG) — every run will exit 2 until it is fixed; nothing to reload or prove" >&2; fail=1 ;;
      *:OPEN) if [ "$UNIT_RUNNING" = yes ]; then echo "   unchanged, confirmed loaded, a run is in flight now; left as it is"
              else action=reprove; why="the unit's last run began and never ended (killed mid-dump?)"; fi ;;
      OK:OK|LOG_OK:OK) echo "   unchanged, confirmed loaded, the unit dumped within 2 h; left as it is" ;;
      OK:NONE)         action=reprove; why="a dump landed within 2 h but the unit's log shows no run (a manual dump? the unit not firing?)" ;;
      NONE:*)          action=reprove; why="no dump in the last 2 h in $dest" ;;
      *:FAIL|LOG_FAIL:*) action=reprove; why="the unit's last run within 2 h failed" ;;
      *)               action=reprove; why="no evidence the unit ran in the last 2 h (destination: $EVIDENCE_MSG)" ;;
    esac
  fi
fi
loaded=no
case "$action" in
  reload)
    echo "   reloading ($why)"
    # never boot the unit out mid-dump (it holds backup.lock); a manual run
    # in the instant between the last probe and the bootout would lose
    # that dump, which the proving run then replaces
    if ! unit_busy_wait backup "$BUSY_BOUND"; then
      echo "   a dump is still running (or its lock cannot be probed); leaving $label as it is — re-run later" >&2
      fail=1
    elif place_unit "$plist_src" "$plist_dst" "$(id -u)" && load_unit "$label" "$domain" "$plist_dst"; then
      loaded=yes
    else
      fail=1
    fi ;;
  reprove)
    echo "   loaded and current, but unproven ($why); proving it, no reload" ;;
esac

# --- prove the unit with one run through launchd ---
# `kickstart`, so the run happens in the agent's OWN launchd session (its
# TCC posture, its environment), not this shell's. launchd runs one
# instance per label, so the unit never races itself: a run already in
# flight IS the run to judge (waited for, not restarted), and the only
# thing that can hold the lock against it is a manual `bash
# snowline-backup.sh` — then (exit 3) the run proves nothing and is fired
# once more after the lock frees; a second step-aside ends as "nothing
# proven". Completion is seen in launchd's run counter and the exit code is
# the agent's; evidence is a dump newer than the run (or the agent's own
# log lines when the destination cannot be read from here).
prove_run() {
  pr_try="$1"
  unit_snapshot "$domain/$label"; runs_before="$UNIT_RUNS"
  run_stamp="$(date -u +%FT%TZ)"; run_epoch="$(date +%s)"
  if [ "$UNIT_RUNNING" = yes ]; then
    # its start is the script's own last "run start" line; failing that
    # (log rotated), the previous run's "run exit" stamp — never a guess
    # that could credit an older run's dumps to this one
    echo "   a run is already in flight; judging that one"
    runs_before=$((UNIT_RUNS - 1))
    # (one pass over the log's stamped lines for both stamps: this run's start is a
    # "run start" NEWER than the last "run exit"; an older one is the
    # previous run's — this run spawned an instant ago and has not written
    # its own yet — and a log with no usable stamp gives a window of 15 min,
    # longer than any run, since the run's dumps are necessarily older than
    # "now")
    started="$(log_lines_since 0 | awk '$2 == "run" && $3 == "start" { s = $1 } $2 == "run" && $3 == "exit" { e = $1 } END { print (s > e ? s : "") }')"
    started_epoch="$(date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$started" +%s 2>/dev/null)" || started_epoch=""
    if [ -n "$started" ] && [ -n "$started_epoch" ]; then run_stamp="$started"; run_epoch="$started_epoch"; else run_epoch=$((run_epoch - 900)); run_stamp="$(date -u -v-15M +%FT%TZ)"; fi
  else
    # bounded: a kickstart of a unit launchd has penalty-boxed can hang
    launchctl kickstart "$domain/$label" & kp=$!
    ki=0; while kill -0 "$kp" 2>/dev/null && [ "$ki" -lt 30 ]; do sleep 1; ki=$((ki + 1)); done
    if kill -0 "$kp" 2>/dev/null; then
      kill "$kp" 2>/dev/null
      echo "   kickstart hung for 30 s (penalty box after spawn failures?); reload the unit: launchctl bootout $domain/$label; re-run this installer" >&2
      fail=1; return
    fi
    if ! wait "$kp"; then
      echo "   kickstart refused; the unit is loaded but unproven — run 'bash $BACKUP_SCRIPT' by hand" >&2
      fail=1; return
    fi
  fi
  code=""; i=0; spawned=no; gone=no
  while [ "$i" -lt "$PROVE_BOUND" ]; do   # 120 s Postgres wait + up to 60 s lock wait per store + the dumps
    unit_snapshot "$domain/$label" || { gone=yes; break; }   # unit gone
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
  if [ "$code" = 3 ]; then
    if [ "$pr_try" = 1 ]; then
      echo "   stepped aside (a manual run held the lock); waiting for it, then firing once more"
      if unit_busy_wait backup "$BUSY_BOUND"; then prove_run 2; return; fi
      if [ "$BUSY_BOUND" -eq 0 ]; then
        echo "   not waiting for it under --deploy; nothing proven — run this installer by itself later" >&2
      else
        echo "   that run is still going after $((BUSY_BOUND / 60)) min; nothing proven — re-run later" >&2
      fi
    else
      echo "   stepped aside twice; nothing proven — re-run later" >&2
    fi
    fail=1; return
  fi
  tail -n 8 "$log_file" 2>/dev/null | sed 's/^/   /'
  if [ "$code" = 0 ]; then
    log_verdict "$run_stamp"
    dump_evidence $(( ($(date +%s) - run_epoch) / 60 + 1 ))
    case "$EVIDENCE" in
      OK)       echo "   OK (exit 0; dump landed in $dest)" ;;
      LOG_OK)   echo "   OK (exit 0; the log shows the dump — destination not checkable from this context: $EVIDENCE_MSG)" ;;
      NONE)     echo "   exit 0 but NO new dump in $dest — not trusting it; check $log_file" >&2; fail=1 ;;
      LOG_FAIL) echo "   exit 0 but the log's last run failed — check $log_file" >&2; fail=1 ;;
      CONFIG_FAIL) echo "   exit 0, but the script now refuses its configuration ($EVIDENCE_MSG) — the next run will exit 2" >&2; fail=1 ;;
      *)        echo "   exit 0, UNCONFIRMED: could not check $dest ($EVIDENCE_MSG) and the log shows no run — verify by hand: ls -t '$dest' | head" >&2; fail=1 ;;
    esac
  elif [ "$gone" = yes ]; then
    echo "   $domain/$label disappeared during the proof (booted out by another process?); nothing proven — re-run this installer" >&2
    fail=1
  elif [ -z "$code" ]; then
    if unit_snapshot "$domain/$label" && [ "$UNIT_RUNNING" = yes ]; then
      echo "   still running after ${i}s; not proven${DEPLOY_NOTE:-} — check $log_file later, or run this installer by itself" >&2
    else
      echo "   no result after ${i}s (never spawned, or killed by a signal) — check $log_file and 'launchctl print $domain/$label'" >&2
    fi
    fail=1
  else
    echo "   FAILED (exit $code) — see $log_file (1: a store failed; 2: cannot run here)" >&2
    fail=1
  fi
}
if [ "$loaded" = yes ] || [ "$action" = reprove ]; then
  echo "== $label (proving run, via launchd)"
  prove_run 1
fi
exit "$fail"
