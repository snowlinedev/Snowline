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

unset CDPATH   # before the `cd` below: a caller's CDPATH would make it print and wander
unset SNOWLINE_OWNER_HOME   # lib.sh honours it for the backup script's sake; an installer is the OWNER's
# shellcheck disable=SC1091
. "$(cd "$(dirname "$0")" && pwd)/launchdaemons/lib.sh"
if [ "$(id -u)" != "$OWNER_UID" ]; then
  echo "run this as the hub owner (uid $OWNER_UID), not root or another account: the unit is the owner's per-user agent and every path here is the owner's" >&2
  exit 64
fi
# the owner's HOME, whatever the caller's was (`sudo -u owner` without -H
# keeps the caller's): the script resolves its destination, log and lock
# from HOME, and the unit runs with the owner's
HOME="$OWNER_HOME"; export HOME

case "${1:-}" in
  --deploy) DEPLOY=yes ;;
  "") DEPLOY=no ;;
  *) echo "unknown argument '$1' (only --deploy is accepted)" >&2; exit 64 ;;
esac
# (a normal run is 120 s of Postgres wait at most, up to 60 s of lock wait
# per store, then dumps of seconds each: 15 min covers that, the 10 min of
# --deploy nearly; a run with a STALLED store may last up to the script's
# own watchdog bounds — 30 min for the dump and 30 more for its
# verification — and then the verdict here is "still running", not a
# failure of the unit)
if [ "$DEPLOY" = yes ]; then BUSY_BOUND=0; PROVE_BOUND=600; DEPLOY_NOTE=" (a --deploy call does not wait longer)"; else BUSY_BOUND=600; PROVE_BOUND=900; DEPLOY_NOTE=""; fi

label="dev.snowline.backup"
domain="gui/$(id -u)"
plist_src="$LAUNCHAGENTS_SRC/$label.plist"
plist_dst="$AGENTS/$label.plist"
log_file="$(backup_log)" || { echo "cannot read StandardOutPath from $LAUNCHAGENTS_SRC/dev.snowline.backup.plist (plutil? malformed plist?)" >&2; exit 1; }
fail=0

# --- guards: the unit runs the canonical script; this install must BE it ---
# The plist carries the canonical checkout path on purpose (a path taken
# from whichever checkout runs this would make a scratch worktree the
# permanent unit and die with EX_CONFIG when it is deleted). So: that file
# must exist, and this install must run from that very checkout (same
# file, not merely equal bytes — it sources the canonical lib.sh and
# backup.env too).
backup_canonical_here || { echo "$BACKUP_GUARD_WHY — NOT installing" >&2; exit 1; }
echo "== $label (per-user agent; unit runs $BACKUP_SCRIPT)"
[ -e "$log_file" ] || touch "$log_file"

# --- evidence helpers: ask the script (same backup.env, same destination) ---
# With this shell's SNOWLINE_BACKUP_* unset: the agent under launchd sees
# only backup.env, and the probes must see the same, or a leftover export
# from a scratch test would make them judge a different folder.
# (the variables cleared are BACKUP_ENV_VARS from lib.sh — the script's
# own list of overrides, so the two cannot drift)
query_env=""; for qv in $BACKUP_ENV_VARS; do query_env="$query_env -u $qv"; done
# shellcheck disable=SC2086
query() { env $query_env /bin/bash "$BACKUP_SCRIPT" "$@"; }
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
    0) EVIDENCE=OK; EVIDENCE_MSG=""; return ;;    # (no reason to carry: stderr here is backup.env's chatter at most)
    1) EVIDENCE=NONE; EVIDENCE_MSG=""; return ;;
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
  # one pass: the last marker's kind, and the last exit code that is a
  # verdict (3, a step-aside, is not one)
  set -- $(log_lines_since "$1" | awk '$2 == "run" && $3 == "start" { l = "start" } $2 == "run" && $3 == "exit" { l = "exit"; if ($4 != 3) v = $4 } END { print l, v }')
  LOG_LAST_EXIT="${2:-}"   # the last verdict code in the window, OPEN or not
  if [ "${1:-}" = start ]; then LOGSAYS=OPEN
  elif [ -z "${2:-}" ]; then LOGSAYS=NONE
  elif [ "$2" = 0 ]; then LOGSAYS=OK
  else LOGSAYS=FAIL; fi
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
  verdict="$UNIT_EXIT"   # launchd's code, or a word for what the code cannot say
  [ "$UNIT_RUNNING" != yes ] && [ "$UNIT_RUNS" -gt 0 ] && [ -z "$UNIT_EXIT" ] && verdict=signal
  # a registration that has never run has no record of its own — unless
  # launchd's counter merely restarted at login: then the log shows a "run
  # exit" newer than this plist's confirmed-loaded marker, and that run WAS
  # this plist's. Only a load with no run since (a reload whose proof was
  # refused or timed out, then a re-run) is "never".
  # (only when launchd reports no exit code at all: a 78 with runs = 0 — a
  # program that could not be spawned — must still reach the reload arm)
  if [ "$UNIT_RUNS" -eq 0 ] && [ "$UNIT_RUNNING" != yes ] && [ -z "$UNIT_EXIT" ]; then
    marker_epoch="$(stat -f %m "$LOCKS_DIR/$label.loaded" 2>/dev/null)" || marker_epoch=0
    marker_stamp="$(date -u -r "$marker_epoch" +%FT%TZ)"
    [ -n "$(log_lines_since "$marker_stamp" | awk '$2 == "run" && $3 == "exit"')" ] || verdict=never
  fi
  case "$verdict" in
    ""|0|2|3) ;;   # (2, "cannot run here", is judged by the evidence below: a refused configuration is reported, not re-kicked; a run in flight is left alone)
    never) action=reprove; why="this registration has never run (an earlier proof did not complete?)" ;;
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
  run_stamp="$(utc_stamp)"; run_epoch="$(date +%s)"
  if [ "$UNIT_RUNNING" = yes ]; then
    # its start is the script's own last "run start" line; failing that
    # (log rotated), the previous run's "run exit" stamp — never a guess
    # that could credit an older run's dumps to this one
    echo "   a run is already in flight; judging that one"
    runs_before=$((UNIT_RUNS - 1))
    # (one pass over the log's stamped lines for both stamps: this run's
    # start is a "run start" NEWER than the last "run exit" AND within the
    # last two hours — an older unmatched start is a run that died without
    # its exit line, not this one; with no usable start — this run spawned
    # an instant ago and has not written its own yet, or the log was
    # rotated — the start is taken as five minutes ago: a run that far in
    # already took its STAMP and is about to land dumps, and no manual run
    # of the last hour is credited)
    started="$(log_lines_since "$(date -u -v-120M +%FT%TZ)" | awk '$2 == "run" && $3 == "start" { s = $1 } $2 == "run" && $3 == "exit" { e = $1 } END { print (s > e ? s : "") }')"
    started_epoch="$(date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$started" +%s 2>/dev/null)" || started_epoch=""
    if [ -n "$started" ] && [ -n "$started_epoch" ]; then run_stamp="$started"; run_epoch="$started_epoch"
    else run_epoch=$((run_epoch - 300)); run_stamp="$(date -u -v-5M +%FT%TZ)"; fi
  else
    # bounded (run_bounded, lib.sh): a kickstart of a unit launchd has
    # penalty-boxed can hang
    run_bounded 30 launchctl kickstart "$domain/$label"; ks_rc=$?
    if [ "$ks_rc" -eq 124 ]; then
      echo "   kickstart hung for 30 s (penalty box after spawn failures?); reload the unit: launchctl bootout $domain/$label; re-run this installer" >&2
      fail=1; return
    elif [ "$ks_rc" -ne 0 ]; then
      echo "   kickstart refused; the unit is loaded but unproven — run 'bash $BACKUP_SCRIPT' by hand" >&2
      fail=1; return
    fi
  fi
  code=""; i=0; spawned=no; gone=no
  while [ "$i" -lt "$PROVE_BOUND" ]; do   # 120 s Postgres wait + up to 60 s lock wait per store + the dumps
    unit_snapshot "$domain/$label" || { gone=yes; break; }   # unit gone
    [ "$UNIT_RUNNING" = yes ] && spawned=yes
    if [ "$UNIT_RUNNING" = no ] && [ "$UNIT_RUNS" -gt "$runs_before" ]; then
      # (debounced: launchd can bump runs an instant before the state line
      # says running, which would hand back the PREVIOUS run's exit code)
      sleep 1
      unit_snapshot "$domain/$label" || { gone=yes; break; }
      [ "$UNIT_RUNNING" = no ] && { code="$UNIT_EXIT"; break; }
      spawned=yes
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
    # (the proving run is known to have ended; an hourly fire that began in
    # the seconds since makes the log OPEN — judge the proving run by the
    # last exit code in the window, not by that new start)
    if [ "$LOGSAYS" = OPEN ] && [ -n "$LOG_LAST_EXIT" ]; then
      if [ "$LOG_LAST_EXIT" = 0 ]; then LOGSAYS=OK; else LOGSAYS=FAIL; fi
    fi
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
