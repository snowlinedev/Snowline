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
OPS_HUB="$(cd "$(dirname "$0")" && pwd)"      # the calling script's directory
LAUNCHDAEMONS_SRC="$OPS_HUB/launchdaemons"
LAUNCHAGENTS_SRC="$OPS_HUB/launchagents"
ICLOUD_ROOT="$OWNER_HOME/Library/Mobile Documents/com~apple~CloudDocs"
BACKUP_DIR="$ICLOUD_ROOT/Snowline/db-backups"   # the backup script's default destination
BACKUP_LOG="$OWNER_HOME/Library/Logs/snowline-backup.log"   # the agent plist's StandardOut/ErrorPath
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

# ---- logging ----
# Helpers and callers log through `log`: one UTC-stamped line. (A shell
# function, so it shadows macOS's /usr/bin/log for everything sourcing this.)
log() { echo "$(date -u +%FT%TZ) $*"; }

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
  waited=0
  while launchctl print "$1" >/dev/null 2>&1; do
    [ "$waited" -ge 30 ] && return 1
    sleep 1
    waited=$((waited + 1))
  done
  return 0
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
# probe (same semantics as flock_fd's sh mode), released at once. It fails
# only while an exclusive holder runs and never leaves the probe owning the
# lock.
# "unknown" = the probe itself could not run (unreadable file, perl
# missing): a guard that cannot tell must refuse, not wave through, so
# callers treat it like held — but without waiting on it.
lock_state() {
  [ -e "$1" ] || { echo free; return; }
  # perl opens the path itself: under /bin/sh a failed `exec 8<` redirection
  # on the special builtin exits the whole script, so no shell-side open here
  /usr/bin/perl -e 'use Fcntl qw(:flock);
    open(my $fh, "<", $ARGV[0]) or exit 2;
    exit 0 if flock($fh, LOCK_SH | LOCK_NB);
    exit(($!{EWOULDBLOCK} || $!{EAGAIN}) ? 1 : 2);' "$1" 2>/dev/null
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
  lock="$LOCKS_DIR/$1.lock"
  bound="${2:-600}"
  step="${3:-5}"
  busy_waited=0
  while :; do
    case "$(lock_state "$lock")" in
      free) return 0 ;;
      unknown) echo "   cannot probe $lock (perl missing? unreadable?); not touching $1" >&2; return 1 ;;
    esac
    if [ "$busy_waited" -ge "$bound" ]; then
      [ "$bound" -eq 0 ] || echo "   $1 is still running after ${busy_waited}s ($lock held)" >&2
      return 1
    fi
    echo "   $1 run in progress; waiting (${busy_waited}s)"
    sleep "$step"
    busy_waited=$((busy_waited + step))
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
# record_loaded <label> <plist> — write that marker. The directory is the
# OWNER's (the backup and autocut locks live there too): when root writes
# it, directory and marker are handed to the owner, or the owner's own
# units could no longer open their locks.
record_loaded() {
  { mkdir -p "$LOCKS_DIR" && cksum < "$2" | cut -d' ' -f1 > "$LOCKS_DIR/$1.loaded"; } || return 1
  if [ "$(id -u)" = 0 ]; then chown "$OWNER_UID" "$LOCKS_DIR" "$LOCKS_DIR/$1.loaded" || return 1; fi
}
# load_unit <label> <domain> <dst-plist> — boot the label out of <domain>,
# wait for it to be gone, bootstrap the plist (a RunAtLoad / KeepAlive unit
# starts right there; nothing is kickstarted: a kick after the bootstrap
# would only kill the instance launchd just spawned, mid-startup) and
# record its checksum as the confirmed-loaded definition.
# Sets UNIT_RESULT: installed | still-old (the old registration was still
# tearing down after 60 s — nothing re-registers it, re-run once it is gone
# — or bootstrap was refused with it still loaded, in which case the OLD
# definition keeps running) | not-loaded (bootstrap refused, nothing loaded
# — a gui-domain unit then loads at the next login).
# Returns 0 only for installed.
load_unit() {
  lu_label="$1"; lu_domain="$2"; lu_dst="$3"
  rm -f "$LOCKS_DIR/$lu_label.loaded"   # whatever runs from here on is unconfirmed until bootstrap succeeds
  launchctl bootout "$lu_domain/$lu_label" 2>/dev/null || true
  # the bootout is issued: the old instance is going down and nothing will
  # re-register it, so give a slow teardown a second 30 s rather than leave
  # the unit with no replacement
  if ! wait_unloaded "$lu_domain/$lu_label" && ! wait_unloaded "$lu_domain/$lu_label"; then
    echo "   old registration of $lu_label still tearing down after 60s; it will NOT come back by itself and the new one is not loaded — re-run once it is gone" >&2
    UNIT_RESULT=still-old
    return 1
  fi
  if ! launchctl bootstrap "$lu_domain" "$lu_dst"; then
    if launchctl print "$lu_domain/$lu_label" >/dev/null 2>&1; then
      echo "   bootstrap of $lu_label refused and an old registration is still loaded (old definition keeps running) — re-run, or reboot" >&2
      UNIT_RESULT=still-old
    else
      echo "   bootstrap of $lu_label refused (no session for $lu_domain?); the plist is installed at $lu_dst and loads when that domain next comes up — nothing runs for $lu_label until then" >&2
      UNIT_RESULT=not-loaded
    fi
    return 1
  fi
  if ! record_loaded "$lu_label" "$lu_dst"; then
    echo "   $lu_label is loaded, but its confirmed-loaded marker could not be written under $LOCKS_DIR — the next install will reload it; check that directory" >&2
  fi
  UNIT_RESULT=installed
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
  label="dev.snowline.$1"
  launchctl bootout "system/$label" 2>/dev/null || true
  if ! wait_unloaded "system/$label"; then
    echo "   daemon $label still registered after 30s; NOT starting the agent on top of it — re-run" >&2
    return 1
  fi
  rm -f "${DAEMONS_DIR:?}/${label:?}.plist"
  disabled="$(ls -1 "$AGENTS/$label.plist.disabled-"* 2>/dev/null | sort | tail -n 1)"
  if [ -f "$AGENTS/$label.plist" ]; then
    [ -z "$disabled" ] || echo "   keeping the live $AGENTS/$label.plist (leaving $disabled aside)"
  elif [ -n "$disabled" ]; then
    mv "$disabled" "$AGENTS/$label.plist"
    chown "$OWNER_UID" "$AGENTS/$label.plist"
    echo "   restored $AGENTS/$label.plist"
  else
    echo "   no .disabled-* agent plist found for $label; daemon removed, nothing loaded" >&2
    return 1
  fi
  if launchctl print "gui/$OWNER_UID/$label" >/dev/null 2>&1; then
    echo "   agent $label already loaded in gui/$OWNER_UID"
    return 0
  fi
  if launchctl bootstrap "gui/$OWNER_UID" "$AGENTS/$label.plist"; then
    return 0
  fi
  echo "   agent plist for $label is on disk but could NOT be loaded into gui/$OWNER_UID (no login session?); it loads at the next login — nothing runs for $label until then" >&2
  return 1
}
