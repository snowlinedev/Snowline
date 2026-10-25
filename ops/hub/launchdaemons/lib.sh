#!/bin/sh
# Shared constants and helpers for the hub's launchd ops scripts. Sourced (not
# executed) by ops/hub/install-daemons.sh, ops/hub/uninstall-daemons.sh,
# ops/hub/install-backup-agent.sh and ops/hub/snowline-backup.sh. POSIX sh. (run-service.sh, the services' boot
# wrapper, deliberately does NOT source this: it keeps its own inline
# Postgres wait so it depends on no file but itself.)

# ---- the hub's units (one definition for install and uninstall) ----
# The four services: system LaunchDaemons, kickstarted after bootstrap.
SERVICES="platform pm governance memory"
# System-only timers: bootstrapped, never kickstarted (a kickstart fires an
# immediate run — for autocut, a ~6-minute cut). No per-user predecessor.
TIMERS="autocut"
# The hourly DB backup is a per-user LaunchAgent on purpose (see
# docs/ops/hub-daemons.md "DB backups"); its tracked plist lives under
# ops/hub/launchagents/ and install-backup-agent.sh installs it.

OWNER_UID=501
# The hub owner's home. The root-run ops scripts need it fixed; the backup
# script sets SNOWLINE_OWNER_HOME=$HOME before sourcing so that, run by any
# user anywhere (a developer testing it), its lock and default destination
# are under that user's home.
OWNER_HOME="${SNOWLINE_OWNER_HOME:-/Users/seanlynch}"
DAEMONS_DIR=/Library/LaunchDaemons           # not DEST: the backup script's dump dir
AGENTS="$OWNER_HOME/Library/LaunchAgents"
unset CDPATH                                 # a caller's CDPATH would make `cd` print, here and in every caller
OPS_HUB="$(cd "$(dirname "$0")" && pwd)"      # the calling script's directory
LAUNCHDAEMONS_SRC="$OPS_HUB/launchdaemons"
LAUNCHAGENTS_SRC="$OPS_HUB/launchagents"
ICLOUD_ROOT="$OWNER_HOME/Library/Mobile Documents/com~apple~CloudDocs"
BACKUP_DIR="$ICLOUD_ROOT/Snowline/db-backups"   # the backup script's default destination
# The backup script's operator overrides (~/.config/snowline/backup.env):
# the script reads them, install-backup-agent.sh clears them from its own
# environment before querying the script — one list for both.
BACKUP_ENV_VARS="SNOWLINE_BACKUP_DIR SNOWLINE_BACKUP_RETENTION_DAYS SNOWLINE_BACKUP_DBS"
# backup_script — the canonical hub checkout's backup script: whatever the
# tracked agent plist runs (its LAST ProgramArguments entry). The plist is
# the single source, deliberately static (a path taken from whichever
# checkout runs an install could be a scratch worktree, dying with
# EX_CONFIG when deleted); install-backup-agent.sh refuses to run from any
# other checkout. A function, not a constant: only the installer needs it,
# and the hourly run should not fork plutil.
backup_script() {
  bs_plist="$LAUNCHAGENTS_SRC/dev.snowline.backup.plist"
  bs_n="$(plutil -extract ProgramArguments raw -o - "$bs_plist" 2>/dev/null)" || return 1
  [ "${bs_n:-0}" -gt 0 ] || return 1
  plutil -extract "ProgramArguments.$((bs_n - 1))" raw -o - "$bs_plist" 2>/dev/null
}

# backup_log — the agent's log file as the tracked plist names it
# (StandardOutPath): the plist is the one source; 1 when it cannot be read.
backup_log() {
  bl="$(plutil -extract StandardOutPath raw -o - "$LAUNCHAGENTS_SRC/dev.snowline.backup.plist" 2>/dev/null)" && [ -n "$bl" ] && printf '%s\n' "$bl"
}

# ---- logging ----
# Helpers and callers log through `log`: one UTC-stamped line. (A shell
# function, so it shadows macOS's /usr/bin/log for everything sourcing this.)
log() { echo "$(date -u +%FT%TZ) $*"; }
# The stamp's grammar, for whoever reads those lines back (awk ERE, no
# interval expressions: macOS awk).
LOG_STAMP_RE='^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z$'

# ---- waits ----
# wait_for_postgres — bounded wait (120 s, 5 s steps) for pg_isready on the
# local socket; returns 0 when ready, 1 after the bound (callers go on
# anyway: the next timer tick retries). Postgres on the hub is the owner's
# per-user LaunchAgent, so anything that can start before the login session
# must wait for it.
wait_for_postgres() {
  waited=0
  until pg_isready -q; do
    if [ "$waited" -ge 120 ]; then
      log "postgres not ready after ${waited}s; continuing anyway (the next tick retries)"
      return 1
    fi
    log "waiting for postgres (${waited}s)"
    sleep 5
    waited=$((waited + 5))
  done
  return 0
}

# unit_snapshot <domain/label> — one launchctl read; sets UNIT_LOADED
# (yes/no), UNIT_RUNNING (yes/no), UNIT_RUNS (launchd's run counter) and
# UNIT_EXIT (the numeric exit code of the last completed run, or empty:
# never ran, or ended by a signal), all describing the same instant.
unit_snapshot() {
  UNIT_LOADED=no; UNIT_RUNNING=no; UNIT_RUNS=0; UNIT_EXIT=""
  us_info="$(launchctl print "$1" 2>/dev/null)" || return 1
  UNIT_LOADED=yes
  if printf '%s\n' "$us_info" | grep -q -E '^[[:space:]]*(state = running|pid = [0-9])'; then
    UNIT_RUNNING=yes
  fi
  UNIT_RUNS="$(printf '%s\n' "$us_info" | sed -n 's/^[[:space:]]*runs = \([0-9][0-9]*\).*/\1/p' | head -n 1)"
  UNIT_RUNS="${UNIT_RUNS:-0}"
  UNIT_EXIT="$(printf '%s\n' "$us_info" | sed -n 's/.*last exit code = \([0-9][0-9]*\).*/\1/p' | head -n 1)"
  return 0
}

# wait_unloaded <domain/label> — after a bootout, wait (up to 30 s) until
# launchd no longer knows the label, so an immediate bootstrap of the same
# label cannot race the teardown ("Operation already in progress").
wait_unloaded() {
  wu_bound="${2:-30}"
  waited=0
  while launchctl print "$1" >/dev/null 2>&1; do
    [ "$waited" -ge "$wu_bound" ] && return 1
    sleep 1
    waited=$((waited + 1))
  done
  return 0
}
# bootout_wait <domain/label> — the ONE teardown: issue the bootout (it may
# not be loaded) and wait for the registration to go, up to 60 s (twice
# wait_unloaded's default): once the bootout is issued nothing re-registers
# the unit, so a slow teardown (a migration finishing) gets the time rather
# than leave the caller with no replacement loaded. 1 = still registered; the caller says what that
# leaves running.
bootout_wait() {
  launchctl bootout "$1" 2>/dev/null || true
  wait_unloaded "$1" 60
}

# ---- unit run locks ----
# A timer that must not be booted out mid-run holds a flock on
# $LOCKS_DIR/<name>.lock for its whole run: autocut for the cut (taken by
# `snowline release autocut`), the backup for the dump (snowline-backup.sh
# opens the file on fd 9 for its lifetime and flocks it). lock_held <path> probes
# a lock without keeping it (a shared non-blocking lock, released at once;
# the backup's acquire retries over a few seconds so a probe cannot make it
# step aside). The kernel drops a flock with its process, so a file left by
# a crash or power loss is simply not held: no pid to trust, nothing to
# take over, no false "in progress" after a reboot.
LOCKS_DIR="$OWNER_HOME/Library/Application Support/Snowline"
BACKUP_LOCK="$LOCKS_DIR/backup.lock"
AUTOCUT_LOCK="$LOCKS_DIR/autocut.lock"
AUTOCUT_LOG="$OWNER_HOME/Library/Logs/snowline-autocut.log"
# flock_fd <fd> <sh|ex> <tries> — the ONE flock implementation (in
# /usr/bin/perl: base macOS, no Command Line Tools stub). Non-blocking;
# with <tries> > 1 it retries once a second. Exit 0 = acquired (the lock
# stays on the fd's open file description, i.e. as long as the caller keeps
# the fd), 1 = held by someone else, 2 = the flock itself failed (fd not
# open, filesystem without flock).
flock_fd() {
  /usr/bin/perl -e 'use Fcntl qw(:flock);
    my ($fd, $mode, $tries) = @ARGV;
    open(my $fh, ($mode eq "ex" ? ">>&=" : "<&="), $fd) or exit 2;
    my $op = ($mode eq "ex" ? LOCK_EX : LOCK_SH) | LOCK_NB;
    for (1..$tries) {
        exit 0 if flock($fh, $op);
        exit 2 unless $!{EWOULDBLOCK} || $!{EAGAIN};
        sleep 1 if $_ < $tries;
    }
    exit 1;' "$1" "$2" "$3" 2>/dev/null
}

# lock_state <path> — prints held / free / unknown: a shared non-blocking
# probe through flock_fd, released at once. It fails only while an
# exclusive holder runs and never leaves the probe owning the lock.
# "unknown" = the probe itself could not run (unreadable file, perl
# missing): a guard that cannot tell must refuse, not wave through, so
# callers treat it like held — but without waiting on it.
lock_state() {
  [ -e "$1" ] || { echo free; return; }
  # a file this user cannot open is "unknown" (a root-owned lock, say) —
  # decided here, because under /bin/sh a failed `exec 8<` on the special
  # builtin exits the shell with status 1, which would read as "held"
  [ -r "$1" ] || { echo unknown; return; }
  # one flock implementation: flock_fd on a read fd opened in a SUBSHELL,
  # so even that exec failure could only end the subshell
  ( { exec 8<"$1"; } 2>/dev/null || exit 2; flock_fd 8 sh 1 )
  case $? in 0) echo free ;; 1) echo held ;; *) echo unknown ;; esac
}
# lock_held <path> — true unless the lock is known to be free.
lock_held() { [ "$(lock_state "$1")" != free ]; }

# unit_busy_wait <name> [bound_s] [step_s] — wait (default up to 10 min, in
# 5 s steps) while <name>.lock is held; the ops scripts call it right before
# booting that unit out. Returns 1 if still held after the bound (the
# caller then skips the unit rather than killing a run) or if the lock
# cannot be probed. A bound of 0 is a plain check.
unit_busy_wait() {
  ubw_lock="$LOCKS_DIR/$1.lock"
  ubw_bound="${2:-600}"
  ubw_step="${3:-5}"
  ubw_waited=0
  while :; do
    case "$(lock_state "$ubw_lock")" in
      free) return 0 ;;
      unknown) echo "   cannot probe $ubw_lock (perl missing? unreadable?); not touching $1" >&2; return 1 ;;
    esac
    if [ "$ubw_waited" -ge "$ubw_bound" ]; then
      [ "$ubw_bound" -eq 0 ] || echo "   $1 is still running after ${ubw_waited}s ($ubw_lock held)" >&2
      return 1
    fi
    echo "   $1 run in progress; waiting (${ubw_waited}s)"
    sleep "$ubw_step"
    ubw_waited=$((ubw_waited + ubw_step))
  done
}
# timer_skip <name> <what> — when <name>'s lock is held (or cannot be
# probed) print why the unit is being left alone and return 0; the caller
# then skips it. <what> names the action to repeat later.
timer_skip() {
  lock_held "$LOCKS_DIR/$1.lock" || return 1
  echo "   $1 is running now (or its lock cannot be probed); leaving dev.snowline.$1 as it is — re-run $2 after it finishes" >&2
  return 0
}

# ---- autocut guard ----
# autocut_guard [--wait] — refuse (exit 75) while an autocut is running: a
# `launchctl bootout system/dev.snowline.autocut` SIGTERMs a running cut (~6
# min, hourly). The cut holds a flock on AUTOCUT_LOCK for its whole run, so
# test it first. `--wait` polls up to 15 min instead. (A fire that starts
# between this check and the bootout is not covered by this guard; the
# install re-checks the lock right before touching the autocut unit.)
autocut_guard() {
  # one probe/wait loop for every unit: bound 0 refuses at once (quietly —
  # the refusal below is the message), --wait polls (15 s steps) up to 15 min
  if [ "${1:-}" = "--wait" ]; then ag_bound=900; else ag_bound=0; fi
  unit_busy_wait autocut "$ag_bound" 15 2>/dev/null && return 0
  echo "refusing: an autocut is running (lock held: $AUTOCUT_LOCK), or its lock cannot be probed." >&2
  echo "  booting the timer out now would kill the cut mid-publish." >&2
  echo "  follow it in $AUTOCUT_LOG, then re-run (or pass --wait)." >&2
  exit 75
}

# ---- placing and loading a unit ----
# place_unit <src-plist> <dst-plist> <owner> — put the plist in place
# (creating the directory, owned by <owner>). launchd holds its own copy of
# a loaded definition, so overwriting the file is harmless; do this BEFORE
# anything is booted out, so nothing is torn down before its replacement is
# on disk. Returns 1 (nothing touched) if the copy fails.
place_unit() {
  pu_dir="$(dirname "$2")"
  # the directory is created (and owned) only when it is missing; an
  # existing one is left as the operator has it
  if ! { { [ -d "$pu_dir" ] || { mkdir -p "$pu_dir" && chown "$3" "$pu_dir"; }; } \
         && cp "$1" "$2" && chown "$3" "$2" && chmod 644 "$2"; }; then
    echo "   could not install $1 -> $2; nothing changed" >&2
    return 1
  fi
}
# unit_current <label> <plist> — is <plist> the definition launchd last
# confirmed loaded for <label>? Bytes on disk alone cannot tell (a load can
# fail with the old registration still running), so load_unit records the
# checksum of what it successfully bootstrapped in $LOCKS_DIR/<label>.loaded
# and this compares against it.
unit_current() {
  [ -f "$2" ] && [ -s "$LOCKS_DIR/$1.loaded" ] \
    && [ "$(cat "$LOCKS_DIR/$1.loaded" 2>/dev/null)" = "$(cksum < "$2" | cut -d' ' -f1)" ]
}
# unit_unchanged <label> <domain> <src-plist> <dst-plist> — the ONE
# leave-alone rule for a unit whose installer consults the marker: the
# tracked plist is byte-identical to the installed one, that is the
# definition launchd last confirmed loaded, and launchd still has it.
# On failure UNIT_UNCHANGED_WHY says which test failed.
unit_unchanged() {
  UNIT_UNCHANGED_WHY=""
  if ! cmp -s "$3" "$4" 2>/dev/null; then UNIT_UNCHANGED_WHY="plist changed"
  elif ! unit_current "$1" "$4"; then UNIT_UNCHANGED_WHY="this plist was never confirmed loaded (a previous load did not complete?)"
  elif ! launchctl print "$2/$1" >/dev/null 2>&1; then UNIT_UNCHANGED_WHY="not loaded"
  fi
  [ -z "$UNIT_UNCHANGED_WHY" ]
}
# backup_canonical_here — the ONE checkout guard for the backup agent: the
# tracked plist names the script by absolute path (launchd runs that file,
# and dies with EX_CONFIG when it is gone), so the file must exist and the
# caller must be running from that very checkout (same file, not merely
# equal bytes — it sources that checkout's lib.sh too). Sets BACKUP_SCRIPT;
# on failure BACKUP_GUARD_WHY says why.
backup_canonical_here() {
  BACKUP_GUARD_WHY=""
  if ! BACKUP_SCRIPT="$(backup_script)"; then
    BACKUP_GUARD_WHY="cannot read the script path from $LAUNCHAGENTS_SRC/dev.snowline.backup.plist (plutil? malformed plist?)"
  elif [ ! -f "$BACKUP_SCRIPT" ]; then
    BACKUP_GUARD_WHY="$BACKUP_SCRIPT is not on this hub (checkout moved? then update the path in the tracked plist); a unit pointing there would fail with EX_CONFIG"
  elif [ ! "$OPS_HUB/snowline-backup.sh" -ef "$BACKUP_SCRIPT" ]; then
    BACKUP_GUARD_WHY="this runs from $OPS_HUB, but the backup unit runs $BACKUP_SCRIPT; run it from the canonical checkout (after pulling)"
  fi
  [ -z "$BACKUP_GUARD_WHY" ]
}
# record_loaded <label> <plist> — write that marker. The directory is the
# OWNER's (the backup and autocut locks live there too): when root writes
# it, directory and marker are handed to the owner, or the owner's own
# units could no longer open their locks.
record_loaded() {
  { mkdir -p "$LOCKS_DIR" && cksum < "$2" | cut -d' ' -f1 > "$LOCKS_DIR/$1.loaded"; } || return 1
  if [ "$(id -u)" = 0 ]; then chown "$OWNER_UID" "$LOCKS_DIR" "$LOCKS_DIR/$1.loaded" || return 1; fi
}
# load_unit <label> <domain> <dst-plist> [norecord] — boot the label out of <domain>,
# wait for it to be gone, bootstrap the plist (a RunAtLoad / KeepAlive unit
# starts right there; nothing is kickstarted: a kick after the bootstrap
# would only kill the instance launchd just spawned, mid-startup) and
# record its checksum as the confirmed-loaded definition.
# Returns 0 when the unit is loaded; otherwise it has said why (the old
# registration still tearing down after 60 s — nothing re-registers it, so
# re-run once it is gone; bootstrap refused with the old one still loaded,
# which keeps running the OLD definition; or bootstrap refused with nothing
# loaded — a gui-domain unit then loads at the next login).
load_unit() {
  lu_label="$1"; lu_domain="$2"; lu_dst="$3"; lu_record="${4:-record}"
  rm -f "$LOCKS_DIR/$lu_label.loaded"   # whatever runs from here on is unconfirmed until bootstrap succeeds
  if ! bootout_wait "$lu_domain/$lu_label"; then
    echo "   old registration of $lu_label still tearing down after 60s; the new plist is on disk (it loads when $lu_domain next comes up) but nothing re-registers it now — re-run once the old one is gone" >&2
    return 1
  fi
  if ! launchctl bootstrap "$lu_domain" "$lu_dst"; then
    if launchctl print "$lu_domain/$lu_label" >/dev/null 2>&1; then
      echo "   bootstrap of $lu_label refused and an old registration is still loaded (old definition keeps running) — re-run, or reboot" >&2
    else
      echo "   bootstrap of $lu_label refused (no session for $lu_domain?); the plist is installed at $lu_dst and loads when that domain next comes up — nothing runs for $lu_label until then" >&2
    fi
    return 1
  fi
  # (the marker is for units whose installer consults unit_current — the
  # timers and the backup agent; the services are always cycled on a
  # deploy and pass `norecord`)
  if [ "$lu_record" != norecord ] && ! record_loaded "$lu_label" "$lu_dst"; then
    echo "   $lu_label is loaded, but its confirmed-loaded marker could not be written under $LOCKS_DIR — the next install will reload it; check that directory" >&2
  fi
  return 0
}

# ---- per-user agent restore (the services' rollback) ----
# restore_agent <name> — boot the system daemon out, remove its plist, put
# the newest `.disabled-*` agent plist back (a live one already there is
# kept) and load it into gui/501 (the agents are RunAtLoad + KeepAlive: the
# bootstrap starts them; no kickstart). Returns 0 when an agent is loaded.
# In that order on purpose: a service runs its whole startup (migrations,
# registration) before binding its port, so two instances must never start
# together.
restore_agent() {
  ra_label="dev.snowline.$1"
  # something to restore FIRST: with neither a live agent plist nor a
  # .disabled-* one, the daemon stays — tearing it down would leave the
  # service with nothing loaded anywhere
  ra_disabled="$(ls -1 "$AGENTS/$ra_label.plist.disabled-"* 2>/dev/null | sort | tail -n 1)"
  if [ ! -f "$AGENTS/$ra_label.plist" ] && [ -z "$ra_disabled" ]; then
    echo "   no agent plist to restore for $ra_label (neither $AGENTS/$ra_label.plist nor a .disabled-* copy); leaving the daemon in place" >&2
    return 1
  fi
  if ! bootout_wait "system/$ra_label"; then
    echo "   daemon $ra_label still tearing down after 60s; NOT starting the agent on top of it — once it is gone NOTHING serves $1 until this rollback is re-run" >&2
    return 1
  fi
  rm -f "${DAEMONS_DIR:?}/${ra_label:?}.plist"
  if [ -f "$AGENTS/$ra_label.plist" ]; then
    [ -z "$ra_disabled" ] || echo "   keeping the live $AGENTS/$ra_label.plist (leaving $ra_disabled aside)"
  else
    mv "$ra_disabled" "$AGENTS/$ra_label.plist"
    chown "$OWNER_UID" "$AGENTS/$ra_label.plist"
    echo "   restored $AGENTS/$ra_label.plist"
  fi
  if unit_snapshot "gui/$OWNER_UID/$ra_label"; then
    if [ "$UNIT_RUNNING" = yes ]; then
      echo "   agent $ra_label already loaded and running in gui/$OWNER_UID"
    else
      # loaded but not running (it lost the port to the daemon and sits in
      # launchd's respawn throttle, or has no KeepAlive): start it now, as
      # the old rollback always did
      launchctl kickstart "gui/$OWNER_UID/$ra_label" && echo "   agent $ra_label was loaded but not running; kickstarted" \
        || echo "   agent $ra_label is loaded but not running and the kickstart failed — start it by hand: launchctl kickstart gui/$OWNER_UID/$ra_label" >&2
    fi
    return 0
  fi
  if launchctl bootstrap "gui/$OWNER_UID" "$AGENTS/$ra_label.plist"; then
    # the restored plist is not tracked here: a RunAtLoad one is started by
    # the bootstrap (a kickstart would only kill it mid-startup); any other
    # needs the kick the old rollback always gave
    if [ "$(plutil -extract RunAtLoad raw -o - "$AGENTS/$ra_label.plist" 2>/dev/null)" != true ]; then
      launchctl kickstart "gui/$OWNER_UID/$ra_label" || echo "   agent $ra_label loaded but not started (no RunAtLoad and the kickstart failed) — start it by hand: launchctl kickstart gui/$OWNER_UID/$ra_label" >&2
    fi
    return 0
  fi
  echo "   agent plist for $ra_label is on disk but could NOT be loaded into gui/$OWNER_UID (no login session?); it loads at the next login — nothing runs for $ra_label until then" >&2
  return 1
}
