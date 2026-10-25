#!/bin/bash
# Hourly dump of EVERY live Snowline Postgres store on the hub. Launched by
# ops/hub/launchagents/dev.snowline.backup.plist (the owner's per-user
# LaunchAgent, installed by ops/hub/install-backup-agent.sh); see
# docs/ops/hub-daemons.md "DB backups". One-shot: launchd's timer fires it on
# schedule, so a failed run is simply a skipped run (no retry storm) — the
# next scheduled run takes a fresh dump.
#
# Since the plugin split there is one database per service. Together they are
# the single source of truth for the whole Snowline layer (scopes, milestones,
# work items, decisions, artifacts, working memory, musher runs, marketing).
# None of it is in git — these dumps are the only way back from disk loss or
# a bad migration. Decision c68ec655 (supersedes e4578136) governs: hourly,
# off-machine (iCloud Drive), custom format for inspect-first pg_restore,
# atomic .partial -> rename, per-store retention. The secrets in
# ~/.config/snowline/*.env are deliberately NOT backed up.
#
# The dumps themselves are sensitive: the platform store holds the live
# replication HMAC secrets (replication_subscriptions.secret,
# replication_inbound_streams.secret/previous_secret), so a dump lets its
# holder forge signed replication envelopes. Dumps are therefore written
# owner-only (umask 077, destination chmod 700) — the same posture as the
# SDK's snapshot route. iCloud Drive carries them to Apple's servers and to
# every device signed into the same account, which is the accepted trade-off
# of decision e4578136 (off-machine durability); do not point SNOWLINE_BACKUP_DIR
# at a shared folder.
#
# Restore one store (to the live DB, destructive):
#   pg_restore --clean --if-exists --no-owner -d snowline_pm <dump>
# Restore (to a scratch DB, to inspect first):
#   createdb snowline_restore && pg_restore --no-owner -d snowline_restore <dump>
set -uo pipefail
# The command line. A query (--print-dest, --check) is quiet: it never logs
# a "run start" / "run exit" line, even when it dies on a refused
# configuration before reaching the dispatch.
ARG1="${1:-}"; ARG2="${2:-}"; ARGC=$#
case "$ARG1" in --print-dest|--check) QUIET_EXIT=yes ;; *) QUIET_EXIT=no ;; esac
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
umask 077

# Which databases to dump: by default EVERY database in the server's catalog
# except the scratch patterns below, so a new service's store is backed up
# from its first hour without anyone editing this file. SNOWLINE_BACKUP_DBS
# (space-separated) replaces that with an explicit list — the operator's
# deliberate choice; stores it leaves out are noted in the log. If the
# catalog cannot be read, the run falls back to the known stores
# (FALLBACK_DBS) and says so, never silently dumping nothing.
FALLBACK_DBS="snowline_platform snowline_pm snowline_governance snowline_memory snowline_musher snowline_marketing"
# Only Snowline's own stores (`snowline_*`) — the hub's cluster may host other
# projects' databases, which are not this job's to dump — minus the scratch
# patterns: test / dry-run / e2e stores, the legacy snowline_dev, and
# `*_restore` (the documented inspect-first restore target).
STORE_RE='^snowline_'
# Anchored as whole name components, so a real store whose name merely
# contains one of these words (snowline_testimonials) is not dropped.
SCRATCH_RE='(_(test|dryrun|e2e|restore)(_|$)|^snowline_dev$)'

# Shared helpers and constants (log, wait_for_postgres, ICLOUD_ROOT,
# BACKUP_DIR, BACKUP_LOCK, LOCKS_DIR); lib.sh only defines things. A checkout
# without it cannot run at all: exit 2, not a store failure.
SNOWLINE_OWNER_HOME="$HOME"
# shellcheck disable=SC1091
. "$(dirname "$0")/launchdaemons/lib.sh" || {
    # the one exit path before the EXIT trap exists: write its line by hand
    echo "$(date -u +%FT%TZ) cannot source $(dirname "$0")/launchdaemons/lib.sh; is this a complete checkout?" >&2
    [ "$QUIET_EXIT" = yes ] || echo "$(date -u +%FT%TZ) run exit 2"
    exit 2
}
# Exit codes: 2 = cannot run at all (destination / iCloud root / unwritable /
# lock / bad configuration — the environment, not the data); 1 = some
# store(s) failed to dump; 3 = another run held the lock (stepped aside).
# install-backup-agent.sh reads them; nothing is rolled back on any of them.
die() { log "$*" >&2; exit 2; }
# Temp files for the catalog read's output and stderr, the dump listing's
# stderr and the write probe: created
# here, in the parent shell (z_dumps runs in subshells, where an assignment
# would never reach the trap), removed on every exit path.
# Every run ends with ONE machine-readable line, "run exit <code>", written
# by the EXIT trap whatever the path (success, a refusal, a step-aside, an
# interrupt — armed before anything below can fail): install-backup-agent.sh
# reads the LAST such line to judge a run when it cannot see the
# destination itself. Queries (--print-dest, --check) do not log one
# (QUIET_EXIT, decided with the command line above).
CATALOG_ERR=""; CATALOG_OUT=""; Z_DUMPS_ERR=""; PROBE=""   # temp files, set below; removed on every exit
on_exit() {
    oe_rc=$?
    rm -f ${CATALOG_ERR:+"$CATALOG_ERR"} ${CATALOG_OUT:+"$CATALOG_OUT"} ${Z_DUMPS_ERR:+"$Z_DUMPS_ERR"} ${PROBE:+"$PROBE"}
    [ "$QUIET_EXIT" = yes ] || log "run exit $oe_rc"
}
trap on_exit EXIT
# A SIGTERM/SIGINT/SIGHUP at ANY point (a bootout during the Postgres wait,
# a logout, a shutdown, an ssh session dropping under a manual run) logs itself; the child in flight (DUMP_PID: the
# catalog read or a dump — bash runs a trap only once the foreground
# command returns, so both run in the background and are waited for) is
# killed, and a dump's partial removed. Armed here, before the first log
# line and the first fork, so a run never vanishes without a log line.
OUT=""; DUMP_PID=""; WATCHDOG_PID=""
DUMP_BOUND_S=1800   # per store; the largest store dumps in well under a minute today
trap 'log "interrupted (SIGTERM/SIGINT/SIGHUP)${OUT:+; removing $OUT.partial}" >&2; [ -z "$DUMP_PID" ] || kill "$DUMP_PID" 2>/dev/null; [ -z "$WATCHDOG_PID" ] || kill "$WATCHDOG_PID" 2>/dev/null; [ -z "$OUT" ] || rm -f "$OUT.partial"; exit 143' TERM INT HUP
[ "$QUIET_EXIT" = yes ] || log "run start"   # the run's real start, for the installer
CATALOG_ERR="$(mktemp -t snowline-backup-psql)" || die "cannot create a temp file (TMPDIR unwritable?)"
CATALOG_OUT="$(mktemp -t snowline-backup-catalog)" || die "cannot create a temp file (TMPDIR unwritable?)"
Z_DUMPS_ERR="$(mktemp -t snowline-backup-find)" || die "cannot create a temp file (TMPDIR unwritable?)"
# Operator overrides live in ~/.config/snowline/backup.env (optional; sourced
# here), NOT in the launchd plist: install-backup-agent.sh replaces the plist with
# the tracked copy whenever it differs, so anything put there would be lost.
# The file supplies DEFAULTS: a variable already set in the environment
# (a scratch test's SNOWLINE_BACKUP_DIR=/tmp/x, say) wins over it.
BACKUP_ENV="$HOME/.config/snowline/backup.env"
# (BACKUP_ENV_VARS, from lib.sh, is the whole list of overrides;
# install-backup-agent.sh clears the same list from its environment before
# querying this script)
if [ -f "$BACKUP_ENV" ]; then
    for ev in $BACKUP_ENV_VARS; do printf -v "pre_$ev" '%s' "${!ev:-}"; done
    # A shell file, sourced under `set -a` like the services' and autocut's
    # env files, so a `PGPORT=5433` line there reaches pg_isready, psql and
    # pg_dump as it does for them (it CAN do anything: a PATH= line there
    # does retarget pg_dump — the operator's call). Stdout is sent to
    # stderr, so a stray echo cannot corrupt the --print-dest / --check
    # protocol the installer parses. The shell options in force before are
    # restored afterwards: a `set -e` in the file would otherwise turn a
    # step-aside into exit 1 and a failed store into an aborted run.
    # A file that EXITS (an `exit` line, or `set -e` plus a failing command)
    # would end this shell mid-source with no reason logged: it is sourced
    # in a throwaway subshell first and must come out the other side — its
    # last command's status is NOT the test (`[ -d /Volumes/X ] && ...` with
    # the volume away returns 1 and is fine). The file therefore runs twice
    # a run; it is meant to hold assignments, not side effects.
    # (and a syntax error first: bash would apply the lines before it and
    # drop the rest — the half-configured run this refuses)
    bash -n "$BACKUP_ENV" 2>/dev/null || die "cannot source $BACKUP_ENV: syntax error (bash -n); refusing to run half-configured"
    [ "$( ( set +u; . "$BACKUP_ENV" >/dev/null 2>&1; echo SOURCED ) 2>/dev/null )" = SOURCED ] \
        || die "cannot source $BACKUP_ENV: it exits when sourced (an exit line? set -e with a failing command?); refusing to run half-configured"
    saved_opts="$(set +o)"
    set -a; set +u   # +u: the file may reference variables this shell does not have
    # shellcheck disable=SC1090
    . "$BACKUP_ENV" >&2
    eval "$saved_opts"
    # the environment's value wins — and says so when the file's lost (the
    # hub's pre-#319 agent plist exported SNOWLINE_BACKUP_DIR itself until
    # install-backup-agent.sh replaced it, which made a backup.env value look
    # ignored)
    for ev in $BACKUP_ENV_VARS; do
        pre="pre_$ev"; pre="${!pre}"
        [ -n "$pre" ] || continue
        [ "$pre" = "${!ev:-}" ] || log "note: $ev from the environment ($pre) overrides $BACKUP_ENV (${!ev:-})" >&2
        printf -v "$ev" '%s' "$pre"
    done
fi
# Off-machine by default: iCloud Drive replicates the dumps off this host, so a
# disk failure / theft doesn't take the backups with it. The default lives in
# lib.sh (BACKUP_DIR) so install-backup-agent.sh judges the same folder. Override
# with SNOWLINE_BACKUP_DIR (in backup.env) to point at a tailnet rsync
# target, an external volume, etc.
DEST="${SNOWLINE_BACKUP_DIR:-$BACKUP_DIR}"
# absolute, or launchd (cwd /) and an operator (cwd anywhere) would resolve
# one setting to two folders (CDPATH is unset by lib.sh)
case "$DEST" in /*) ;; *) die "SNOWLINE_BACKUP_DIR must be an absolute path: '$DEST'" ;; esac
RETENTION_DAYS="${SNOWLINE_BACKUP_RETENTION_DAYS:-30}"
case "$RETENTION_DAYS" in ''|*[!0-9]*) die "SNOWLINE_BACKUP_RETENTION_DAYS must be a whole number of days, at least 1: '$RETENTION_DAYS'" ;; esac
[ "$RETENTION_DAYS" -ge 1 ] || die "SNOWLINE_BACKUP_RETENTION_DAYS must be at least 1: '$RETENTION_DAYS'"
# parse_dump_name <path> — the ONE grammar of this script's file names,
# <store>-YYYYMMDD-HHMMSS[Z].dump, optionally iCloud-evicted to
# .<name>.icloud: sets NAME_STORE and NAME_STAMP, or fails for any other
# name. (The pre-#319 names lack the Z and are local time — a few hours
# off, which the 30-day retention does not care about.) A bash regex: no
# fork per file.
DUMP_NAME_RE='^(.*/)?(\.)?([A-Za-z0-9_]+)-([0-9]{8}-[0-9]{6})Z?\.dump(\.icloud)?$'
parse_dump_name() {
    [[ $1 =~ $DUMP_NAME_RE ]] || return 1
    # a dump is NAME.dump; its evicted placeholder is .NAME.dump.icloud —
    # a hidden .dump or a visible .icloud is neither (a hand-made copy, an
    # editor backup) and must not count as evidence or toward the kept three
    { [ -n "${BASH_REMATCH[2]}" ] && [ -n "${BASH_REMATCH[5]}" ]; } || { [ -z "${BASH_REMATCH[2]}" ] && [ -z "${BASH_REMATCH[5]}" ]; } || return 1
    NAME_STORE="${BASH_REMATCH[3]}"; NAME_STAMP="${BASH_REMATCH[4]}"
}
# z_dumps [all] — one "<store>\t<stamp>\t<path>" line per dump or
# placeholder in DEST, every store: Z-named
# (UTC) ones by default, pre-#319 local-time names too with `all`. The ONE
# listing --check (evidence: Z names only, written by this script) and the
# prune (retention: every name) share. find's stderr goes to Z_DUMPS_ERR
# for the caller to inspect.
TAB=$'\t'
z_dumps() {
    if [ "${1:-}" = all ]; then zd_tail=''; else zd_tail='Z'; fi   # all: Z names and pre-#319 names alike
    find "$DEST" -maxdepth 1 \( -name "*-[0-9]*${zd_tail}.dump" -o -name ".*-[0-9]*${zd_tail}.dump.icloud" \) -type f 2>"$Z_DUMPS_ERR" \
    | while IFS= read -r zf; do
        parse_dump_name "$zf" || continue
        printf '%s\t%s\t%s\n' "$NAME_STORE" "$NAME_STAMP" "$zf"
    done
}

# has_word <word> <list...> — is <word> one of the space-separated words?
has_word() { hw="$1"; shift; case " $* " in *" $hw "*) return 0 ;; esac; return 1; }

# SNOWLINE_BACKUP_DBS, normalised ONCE (split on any whitespace, re-joined
# with single spaces; the positional parameters are never touched) so
# --check and the run agree on membership; validated just below, before
# anything side-effecting.
# No globbing from here on, for the WHOLE script: every unquoted expansion
# below is a store list, and a `*` in one must never expand against the
# caller's cwd (launchd's is /, an operator's is anywhere) — the accepted
# set and the dumped set would depend on where the script was started.
set -f
DBS_EXPLICIT=""
for vn in ${SNOWLINE_BACKUP_DBS:-}; do DBS_EXPLICIT="$DBS_EXPLICIT $vn"; done
DBS_EXPLICIT="${DBS_EXPLICIT# }"
# ... and validated here, cheap and side-effect free, so a query and a run
# refuse the same bad value: each name must be a plain identifier (the
# catalog query applies the same rule in SQL); a whitespace-only value is
# "unset", not "dump nothing".
for vn in $DBS_EXPLICIT; do
    case "$vn" in
        *[!A-Za-z0-9_]*) die "SNOWLINE_BACKUP_DBS entry is not a database name: '$vn'" ;;
    esac
done

# store_wanted <name> — the ONE store rule, for the catalog pass and --check
# alike: a snowline_* name that is not a scratch one. A bash regex match: no
# fork per name.
store_wanted() {
    [[ $1 =~ $STORE_RE ]] && ! [[ $1 =~ $SCRATCH_RE ]]
}

# canon_dest — once the destination exists, use its real path: `find
# "$DEST" -maxdepth 1` on a SYMLINK (an override pointing at a volume) sees
# nothing at all, and a trailing slash in an override would put `//` in
# every dump path.
canon_dest() {
    cd_real="$(cd "$DEST" 2>/dev/null && pwd -P)" && [ -n "$cd_real" ] || die "cannot enter the backup destination $DEST (no execute permission on it?)"
    DEST="$cd_real"
}
# dest_state — the ONE destination rule, for --check and the run alike:
# prints ok (exists), no-leaf (absent, but under a present iCloud root: may
# be created), unreadable (this context is denied the stat), not-dir (a
# file or dangling symlink in the way), or missing (iCloud root absent, or a non-iCloud destination
# that is not there — never created here: a destination that is itself a
# mount point would otherwise be silently recreated as a plain local
# directory while the disk is unplugged).
dest_state() {
    if [ -d "$DEST" ]; then echo ok; return; fi
    # the errno FIRST: a path this context is DENIED the stat of (TCC: ssh
    # without Full Disk Access under the iCloud root — the agent in its
    # login session is not denied) is unreadable whether or not the root
    # above it happens to be visible; only a path that is NOT THERE is then
    # a leaf to create (under a present iCloud root) or missing
    # (the trailing slash makes ls follow a symlinked destination to its
    # target, whose errno is the one that matters)
    case "$(LC_ALL=C ls -d "$DEST/" 2>&1 >/dev/null)" in
        *"Operation not permitted"*|*"Permission denied"*) echo unreadable; return ;;   # EPERM (TCC) or EACCES
    esac
    # something that is not a directory in the way (a file, a dangling
    # symlink after a volume rename) is neither absent nor creatable
    if [ -e "$DEST" ] || [ -L "$DEST" ]; then echo not-dir; return; fi
    case "$DEST" in
        "$ICLOUD_ROOT"/*) [ -d "$ICLOUD_ROOT" ] && { echo no-leaf; return; } ;;
    esac
    echo missing
}

# Queries install-backup-agent.sh makes by running this script AS THE OWNER
# (so backup.env, TCC consent and the destination are exactly this
# script's):
#   --print-dest        print the resolved destination
#   --check <minutes>   exit 0 if a dump (or its iCloud-evicted placeholder)
#                       newer than <minutes> exists in the destination,
#                       1 if none, 2 if it cannot be read from here
#                       (UNREADABLE) or does not exist at all (MISSING)
case "$ARG1" in
    --print-dest)
        [ "$ARGC" -eq 1 ] || die "--print-dest takes no further argument"
        [ "$(dest_state)" != ok ] || canon_dest   # the folder a run would use, not the override's spelling
        printf '%s\n' "$DEST"
        exit 0
        ;;
    --check)
        # A dump counts by the UTC stamp in its NAME (this script writes
        # <store>-YYYYMMDD-HHMMSSZ.dump), not by mtime: an iCloud-evicted
        # placeholder carries eviction time, and old pre-Z names are local
        # time and are ignored. The stamps are fixed-width UTC, so they
        # compare as strings against one cutoff stamp — no fork per file.
        # Prints OK / NONE / UNREADABLE on stdout as well as exiting 0 / 1 /
        # 2, so a caller can tell "no dump" from "could not even run".
        [ "$ARGC" -eq 2 ] || die "--check takes exactly one argument, a number of minutes"
        mins="$ARG2"
        case "$mins" in ''|*[!0-9]*) echo "--check needs a number of minutes" >&2; exit 2 ;; esac
        # Same destination rule as a run (dest_state): a leaf that does not
        # exist yet under a present iCloud root is "no dump yet" (NONE); a
        # denied stat is UNREADABLE (to be judged from the log: the agent in
        # its login session is not denied); a path that is not there — iCloud
        # Drive signed out, a volume unplugged — is MISSING.
        case "$(dest_state)" in
            ok) canon_dest ;;
            no-leaf) echo NONE; exit 1 ;;
            unreadable) echo "stat of $DEST denied in this context (Full Disk Access?)" >&2; echo UNREADABLE; exit 2 ;;
            not-dir) echo "$DEST exists but is not a directory (a file or a dangling symlink in the way)" >&2; echo MISSING; exit 2 ;;
            *) echo "$DEST is missing (volume unplugged? iCloud Drive signed out?)" >&2; echo MISSING; exit 2 ;;
        esac
        # (a cutoff that cannot be computed is a refused argument — a die,
        # nothing on stdout — not "the destination is unreadable")
        cutoff="$(date -u -v-"${mins}"M +%Y%m%d-%H%M%S 2>/dev/null)" && [ -n "$cutoff" ] \
            || die "--check: cannot compute a cutoff $mins minutes back"
        # only the stores this job dumps count: the configured list when
        # SNOWLINE_BACKUP_DBS is set, else the catalog rule (store_wanted) —
        # a scratch or foreign dump put in the folder by hand is not evidence
        found=no
        while IFS="$TAB" read -r store s f; do
            [ -n "$f" ] || continue
            if [ -n "$DBS_EXPLICIT" ]; then
                has_word "$store" $DBS_EXPLICIT || continue
            else
                store_wanted "$store" || continue
            fi
            [ "$s" \< "$cutoff" ] || found=yes
        done <<EOF_Z
$(z_dumps)
EOF_Z
        # (a dump found is evidence whatever else find tripped over — an
        # entry iCloud evicted between readdir and stat; only an EMPTY
        # listing with errors is "cannot read from here")
        [ "$found" = yes ] && { echo OK; exit 0; }
        if [ -s "$Z_DUMPS_ERR" ]; then tail -n 1 "$Z_DUMPS_ERR" >&2; echo UNREADABLE; exit 2; fi   # the reason, for the installer
        echo NONE; exit 1
        ;;
    "") [ "$ARGC" -eq 0 ] || die "an empty argument is not a run (this script takes no arguments except --print-dest and --check <minutes>); refusing to run" ;;
    *) die "unknown argument '$ARG1' (this script takes no arguments except --print-dest and --check <minutes>); refusing to run" ;;
esac
# --- run lock: a flock on $BACKUP_LOCK, held for the whole run ---
# fd 9 is opened on the lock file for the life of this process; flock_fd
# (lib.sh, perl) takes the flock on that inherited fd (the lock lives on the
# open file description, so it stays held after the helper exits, for as
# long as this script keeps fd 9). The kernel drops it on exit or crash: no stale
# lock, no pid to trust. lib.sh's unit_busy_wait probes the same file before
# the ops scripts boot this unit out (a probe holds a shared lock for an
# instant, so the acquire retries over a few seconds). A second run while
# one holds the lock (a manual or proving run racing the hourly fire) steps aside with
# exit 3, which install-backup-agent.sh never mistakes for "dumps landed"; any
# other flock error (an fs without flock, a bad fd) is exit 2.
mkdir -p "$(dirname "$BACKUP_LOCK")" || die "cannot create $(dirname "$BACKUP_LOCK") for the run lock"
exec 9>>"$BACKUP_LOCK" || die "cannot open the run lock $BACKUP_LOCK (root-owned from a sudo run?); fix its ownership"
# flock_fd (lib.sh) flocks the inherited fd 9 in place, so the lock
# outlives the helper; five tries a second apart, because a lock_state probe
# holds the file for an instant while a real run holds it for minutes.
flock_fd 9 ex 5
case $? in
    0) ;;
    1) log "another backup run holds $BACKUP_LOCK; stepping aside"; exit 3 ;;
    127) die "cannot take the run lock: /usr/bin/perl is missing (the installers cannot probe the lock without it either; two unlocked runs could collide)" ;;
    *) die "cannot take the run lock $BACKUP_LOCK (flock failed)" ;;
esac

# --- destination: refuse to write "backups" that never leave the machine ---
# `mkdir -p` of the full default path would silently recreate the iCloud
# folder as a plain local directory when iCloud Drive is not yet mounted or
# the account was removed, and every run would then log success while
# nothing is replicated. Only the leaf may be created; the iCloud root must
# already exist. This is a presence check, not a sync check: macOS can keep
# the folder (and its contents) when iCloud Drive is turned off with "keep a
# copy", and nothing here can tell that apart from a healthy mount. Confirm
# off-machine arrival from another device now and then (docs/ops/hub-daemons.md).
# The one rule (dest_state): under a verified iCloud root the leaf folders
# may be created freely; any other destination (external volume, rsync
# target) must already exist and is never created here.
case "$(dest_state)" in
    ok) canon_dest ;;
    no-leaf) mkdir -p "$DEST" || die "cannot create backup destination: $DEST"; canon_dest ;;
    unreadable) die "backup destination cannot be reached from this context: $DEST (stat denied — Full Disk Access / iCloud Drive consent for this session?)" ;;
    not-dir) die "backup destination exists but is not a directory: $DEST (a file or a dangling symlink in the way)" ;;
    *) die "backup destination missing: $DEST (iCloud Drive root absent, or a volume not mounted — it is never created by this script)" ;;
esac
# A real create, not `[ -w ]`: access(2) does not see a TCC denial (a
# system-domain process without iCloud Drive consent) or a full/read-only
# volume, and those must be exit 2 ("cannot run here"), not a per-store 1.
PROBE="$DEST/.probe.$$"
( : > "$PROBE" ) 2>/dev/null || die "backup destination not writable: $DEST (TCC consent? full or read-only volume?)"
rm -f "$PROBE"; PROBE=""
# Owner-only posture for the folder and for Snowline's dumps in it (another
# project's files in a shared destination are not this job's): new dumps are
# written under umask 077; older ones (0644 before this script enforced it)
# and dumps that iCloud re-materialises are tightened each run. (This walk,
# the prune's and the sweep's each stat every entry: at steady state
# ~4,300 files, so some 13,000 stats an hour — fine for a local folder,
# worth knowing before adding a fourth.) Best effort on an iCloud folder,
# but say so.
chmod 700 "$DEST" 2>/dev/null || log "WARNING: could not chmod 700 $DEST" >&2
# (`-exec … {} +` runs nothing when nothing matches: one walk, no chmod
# unless a loose file exists)
# (this job's stores: Snowline's and any the explicit list names)
own_names=(-name 'snowline_*.dump')
# (and the same stores' staging files and evicted placeholders, for the sweep)
own_stale=(-name 'snowline_*-[0-9]*.dump.partial' -o -name '.snowline_*-[0-9]*.dump.partial.icloud')
for db in $DBS_EXPLICIT; do
    own_names+=(-o -name "$db-*.dump")
    own_stale+=(-o -name "$db-[0-9]*.dump.partial" -o -name ".$db-[0-9]*.dump.partial.icloud")
done
find "$DEST" -maxdepth 1 \( "${own_names[@]}" \) -type f -perm +044 -exec chmod 600 {} + 2>"$Z_DUMPS_ERR" \
    || log "WARNING: the permission sweep of $DEST hit an error ($(tail -n 1 "$Z_DUMPS_ERR")); a loose dump may remain" >&2

# --- wait for Postgres (bounded; see lib.sh) ---
wait_for_postgres || true

# --- the store list: the server's catalog is the authority ---
# `-X` so a ~/.psqlrc (\timing etc.) cannot inject tokens into the list.
# The catalog read and the scratch filter are separate steps so "server
# unreachable" (fallback + WARNING) is distinguishable from "every database
# is scratch" (nothing to dump, which is then reported per store).
CATALOG_OK=yes
# Only plain-identifier names (a quoted name with a space or dash would
# split into bogus stores; the same rule as for SNOWLINE_BACKUP_DBS), and
# only databases this role can actually connect to — but a snowline_* store
# that is parked (datallowconn = false) or lost CONNECT is NAMED in the log,
# never silently dropped from the backup set: the query returns
# "<name>|<connectable>" and the unconnectable ones are reported below.
# Bounded (a server that accepts connections but stalls the query must not
# hold the run — or the TERM trap — for ever) and backgrounded like the
# dumps, so a bootout mid-read still gets its "run exit" line.
PGCONNECT_TIMEOUT=15 PGOPTIONS='-c statement_timeout=30000' \
    psql -X -At -d postgres -c "select datname, (datallowconn and has_database_privilege(datname, 'CONNECT')), (datname ~ '^[A-Za-z0-9_]+\$') from pg_database where not datistemplate and datname <> 'postgres' order by 1" > "$CATALOG_OUT" 2>"$CATALOG_ERR" 9>&- &
DUMP_PID=$!
wait "$DUMP_PID" || CATALOG_OK=no
DUMP_PID=""
# One pass over the rows: the store rule (store_wanted) applied once per
# name, connectable names to LIVE, the others to UNCONNECTABLE; a wanted
# name that is not a plain identifier (a quoted name with a dash, say) is
# NAMED as failed below, never silently dropped — this script only dumps
# names it can pass around safely. (The row is read with `|` as the
# separator; a name containing one would be mangled here, and is in any
# case not an identifier.)
LIVE=""; UNCONNECTABLE=""; NONIDENT=""; SCRATCHED=""
while IFS='|' read -r cat_name cat_ok cat_ident; do
    [ -n "$cat_name" ] || continue
    # a name on the explicit list is classified whatever its pattern (it
    # will be dumped: a parked one must be named as unconnectable, not fail
    # blind); otherwise a snowline_* name the scratch rule drops is NAMED
    # once a run below, so a real store with an unlucky name is not missed
    # until restore time
    if has_word "$cat_name" $DBS_EXPLICIT; then :
    elif [[ $cat_name =~ $STORE_RE ]] && [[ $cat_name =~ $SCRATCH_RE ]]; then SCRATCHED="$SCRATCHED $cat_name"; continue
    else store_wanted "$cat_name" || continue
    fi
    if [ "$cat_ident" != t ]; then NONIDENT="$NONIDENT $cat_name"
    elif [ "$cat_ok" = t ]; then LIVE="$LIVE $cat_name"; else UNCONNECTABLE="$UNCONNECTABLE $cat_name"; fi
done < "$CATALOG_OUT"
LIVE="${LIVE# }"; UNCONNECTABLE="${UNCONNECTABLE# }"
[ -z "$SCRATCHED" ] || log "note: not dumping scratch store(s) (SCRATCH_RE):${SCRATCHED}"
# A store this role cannot connect to is a FAILED store, not a footnote:
# the run ends incomplete (exit 1) so launchd, the installer and the "run
# exit" line all say the backup is not whole — unless an explicit
# SNOWLINE_BACKUP_DBS leaves it out, which is the operator's way of saying
# "parked on purpose".
FAILED=""
for uc in $UNCONNECTABLE; do
    if [ -n "$DBS_EXPLICIT" ] && ! has_word "$uc" $DBS_EXPLICIT; then continue; fi
    log "FAILED to back up ${uc}: this role cannot connect to it (parked? CONNECT revoked?)" >&2
    FAILED="$FAILED $uc"
done
for ni in $NONIDENT; do
    [ -z "$DBS_EXPLICIT" ] || continue   # an explicit list names what it wants; it cannot name this
    log "FAILED to back up '${ni}': its name is not a plain identifier (letters, digits, _), which this script does not handle — rename it, or back it up by hand" >&2
    FAILED="$FAILED $ni"
done
if [ "$CATALOG_OK" != yes ]; then
    if [ -n "$DBS_EXPLICIT" ]; then catalog_clause="the connectability check and the exclusion note are skipped this run"; else catalog_clause="dumping the fallback list only: $FALLBACK_DBS"; fi
    log "WARNING: could not list databases from the server ($(tr '\n' ' ' < "$CATALOG_ERR")); $catalog_clause" >&2
fi
if [ -n "$DBS_EXPLICIT" ]; then
    # (an unconnectable name on the list was already recorded as FAILED
    # above; it is not dumped a second time to fail twice)
    DBS=""
    for db in $DBS_EXPLICIT; do
        has_word "$db" $UNCONNECTABLE || DBS="$DBS $db"
    done
    DBS="${DBS# }"
    EXCLUDED=""
    for db in $LIVE; do
        has_word "$db" $DBS || EXCLUDED="$EXCLUDED $db"
    done
    [ -z "$EXCLUDED" ] || log "note: SNOWLINE_BACKUP_DBS excludes live store(s):${EXCLUDED}" >&2
elif [ "$CATALOG_OK" = yes ] && [ -n "$LIVE" ]; then
    DBS="$LIVE"
elif [ "$CATALOG_OK" = yes ] && [ -n "$FAILED" ]; then
    # every store is there but unconnectable: already recorded as FAILED
    # above, nothing to dump, the run ends incomplete — a readable catalog
    # is authoritative, no fallback pass to fail the same stores twice
    DBS=""
elif [ "$CATALOG_OK" = yes ]; then
    # Readable catalog with no usable snowline_* store: the wrong cluster
    # (PGHOST / PGPORT leaking in, a fresh cluster before migration), or
    # every name was filtered. Never exit 0 with nothing dumped: try the
    # known stores, which then fail loudly (exit 1).
    DBS="$FALLBACK_DBS"
    log "WARNING: the catalog holds no usable snowline_* store (wrong cluster?); trying the fallback list: $DBS" >&2
else
    DBS="$FALLBACK_DBS"   # (warned above)
fi

# UTC stamp so file names sort monotonically (no DST fold) and match the log.
STAMP="$(date -u +%Y%m%d-%H%M%SZ)"

# DBS is iterated with globbing off (names are validated above or
# catalog-sourced). A SIGTERM mid-dump (the unit booted out, a logout, a
# shutdown) logs itself and removes the partial it was writing, so the log
# shows a kill rather than a hang.
STORES=0
# The retention cutoff: one stamp, compared against each dump's name stamp
# as a string (fixed-width UTC; no fork per file).
PRUNE_CUTOFF="$(date -u -v-"${RETENTION_DAYS}"d +%Y%m%d-%H%M%S 2>/dev/null)" || PRUNE_CUTOFF=""
[ -n "$PRUNE_CUTOFF" ] || log "WARNING: cannot compute the retention cutoff for $RETENTION_DAYS days; NOT pruning this run (the folder grows until this is fixed)" >&2
# Retention, ONE pass over the folder for EVERY store found there — not just
# this run's list, or a store dropped from the list (SNOWLINE_BACKUP_DBS, a
# retired service) would accrue forever and fill the destination. Whatever
# the age, the newest KEEP_MIN dumps of a store are never pruned: a store
# whose dump keeps failing must not lose its last good copy once it ages
# out. The legacy snowline_dev history is left alone. Runs BEFORE the dumps
# so a full destination can free itself.
KEEP_MIN=3
prune_all() {
    [ -n "$PRUNE_CUTOFF" ] || return 0   # no cutoff, no pruning (never over-delete; warned above)
    # the stores this job may dump — store_wanted's rule (STORE_RE and not
    # SCRATCH_RE, which spares the legacy snowline_dev history) or a name on
    # the explicit list — and no other: a scratch store's dumps or another
    # project's in a shared destination are not this job's to prune
    PRUNE_STUCK="$(z_dumps all | sort -t "$TAB" -k1,1 -k2,2r \
    | awk -F"$TAB" -v cutoff="$PRUNE_CUTOFF" -v keep="$KEEP_MIN" -v re="$STORE_RE" -v scratch="$SCRATCH_RE" -v explicit=" $DBS_EXPLICIT " '
        index(explicit, " " $1 " ") == 0 && ($1 !~ re || $1 ~ scratch) { next }
        $1 != store { store = $1; n = 0 }
        { n++; if (n > keep && $2 < cutoff) print $3 }' \
    | while IFS= read -r pa_f; do rm -f "$pa_f" 2>/dev/null || printf '%s\n' "${pa_f##*/}"; done)"
    # (a listing that failed part-way prunes too little; a dump this user
    # cannot remove — root-owned from a sudo run — would otherwise sit there
    # unpruned: both unmentioned for ever unless said here)
    [ -s "$Z_DUMPS_ERR" ] && log "WARNING: the retention listing of $DEST hit an error ($(tail -n 1 "$Z_DUMPS_ERR")); less than due may have been pruned" >&2
    [ -z "$PRUNE_STUCK" ] || log "WARNING: could not remove $(printf '%s\n' "$PRUNE_STUCK" | wc -l | tr -d ' ') expired dump(s) in $DEST (owned by another user? a sudo run?) — remove by hand: $(printf '%s\n' "$PRUNE_STUCK" | head -n 3 | tr '\n' ' ')" >&2
    return 0
}
# bash runs a trap only once the foreground command returns, so pg_dump is
# started in the background and waited for: `wait` is interruptible, the
# TERM/INT handler armed at the top runs at once, kills the dump and
# removes its partial.
# Prune first (prune_all above: every store in the folder, newest three
# kept, iCloud `.NAME.icloud` placeholders by the same rule — age is the
# stamp in the NAME, a placeholder's mtime being eviction time). Non-fatal:
# a find hiccup must not fail the run.
# run_bounded <seconds> <command...> — the command in the background with
# the run lock's fd closed (9>&-: a child orphaned by a SIGKILL of this
# shell must not hold the lock), killed after <seconds> by a watchdog whose
# own sleep dies with it (an orphan sleep would outlive every run); the
# command's status, 143 when the watchdog struck. DUMP_PID/WATCHDOG_PID are
# what the TERM trap kills.
run_bounded() {
    rb_bound="$1"; shift
    "$@" 9>&- &
    DUMP_PID=$!
    ( trap 'kill "$wd_sleep" 2>/dev/null; exit 0' TERM; sleep "$rb_bound" & wd_sleep=$!; wait "$wd_sleep"; kill "$DUMP_PID" 2>/dev/null ) >/dev/null 2>&1 9>&- &
    WATCHDOG_PID=$!
    wait "$DUMP_PID"; rb_rc=$?
    DUMP_PID=""
    kill "$WATCHDOG_PID" 2>/dev/null; wait "$WATCHDOG_PID" 2>/dev/null; WATCHDOG_PID=""
    return "$rb_rc"
}
prune_all || true
# One more walk for two things. A .partial (or a write probe, or iCloud's
# evicted placeholder of either) belongs to a run that died without its
# trap (SIGKILL, power loss): no live run owns it, so it is swept here, before the dumps,
# and a full destination frees itself — only THIS job's stores' staging
# names (own_stale: Snowline's and the explicit list's, the pre-#319
# local-time stamp included so the old script's last staging file goes
# too): another job's staging file in a shared destination is left alone,
# and not nagged about. And files outside the dump grammar
# (an iCloud conflict copy "NAME 2.dump", a hand-made .bak) are neither
# counted nor pruned: say so once a run, or the bounded folder grows
# unbounded unnoticed.
# (holding the lock, this run is the only one: every such file is an
# orphan now, whatever its age)
UNREC="$(find "$DEST" -maxdepth 1 \( -type f \( "${own_stale[@]}" -o -name '.probe.*' -o -name '..probe.*.icloud' \) -delete \) \
             -o \( -type f ! -name '*.partial' ! -name '.probe.*' ! -name '.*.partial.icloud' ! -name '..probe.*.icloud' ! -name '.DS_Store' -print \) 2>"$Z_DUMPS_ERR" \
    | while IFS= read -r uf; do parse_dump_name "$uf" || printf '%s\n' "${uf##*/}"; done)"
[ -s "$Z_DUMPS_ERR" ] && log "WARNING: the stale-file sweep in $DEST hit an error ($(tail -n 1 "$Z_DUMPS_ERR"))" >&2
[ -z "$UNREC" ] || log "WARNING: $(printf '%s\n' "$UNREC" | wc -l | tr -d ' ') file(s) in $DEST outside the dump name grammar — neither counted nor pruned, tidy by hand: $(printf '%s\n' "$UNREC" | head -n 3 | tr '\n' ' ')" >&2
for DB in $DBS; do
    STORES=$((STORES + 1))
    OUT="$DEST/${DB}-${STAMP}.dump"
    # Dump to a .partial name, verify the archive is readable, then atomically
    # rename — an interrupted or truncated dump (machine sleep, OOM, iCloud
    # hiccup) never leaves a file that looks like a complete backup. One store
    # failing must not skip the others: record it and keep going.
    # --lock-wait-timeout: a dump wants ACCESS SHARE on every table; a service
    # mid-migration (ACCESS EXCLUSIVE) must not be blocked by it for long —
    # better a store failure this hour than a stalled migration.
    # (9>&-: the child must not inherit the run lock's fd, or a pg_dump
    # orphaned by a SIGKILL of this shell would hold the lock until it ends)
    # The dump and its verification each run under run_bounded (above): a
    # backend that hangs after taking its locks, or a read or write that
    # stalls on the destination, would otherwise hold the run lock for ever
    # and every later hourly run would step aside — exit 3, "not a verdict"
    # — in silence.
    run_bounded "$DUMP_BOUND_S" pg_dump -Fc --lock-wait-timeout=60000 "$DB" > "$OUT.partial"; dump_rc=$?
    [ "$dump_rc" -ne 143 ] || log "FAILED to back up ${DB}: the dump ran past ${DUMP_BOUND_S}s and was killed (a hung backend? a stalled destination?)" >&2
    if [ "$dump_rc" -eq 0 ]; then
        run_bounded "$DUMP_BOUND_S" pg_restore -l "$OUT.partial" > /dev/null; verify_rc=$?
        [ "$verify_rc" -ne 143 ] || log "FAILED to back up ${DB}: verifying the archive ran past ${DUMP_BOUND_S}s and was killed (a stalled destination?)" >&2
    else
        verify_rc=1
    fi
    if [ "$verify_rc" -eq 0 ]; then
        # The archive is good: from here the trap must not remove it, so OUT
        # is cleared BEFORE the rename — a signal between the two leaves a
        # complete .partial for the sweep, never a landed dump reported as
        # removed.
        LANDED="$OUT"; OUT=""
        if mv "$LANDED.partial" "$LANDED"; then
            # Size is best-effort (an iCloud file can be evicted to a
            # placeholder the instant it lands, making du error).
            SIZE="$(du -h "$LANDED" 2>/dev/null | awk '{ print $1 }' || true)"
            log "backed up ${DB} -> ${LANDED} (${SIZE:-size unknown})"
        else
            rm -f "$LANDED.partial"
            log "FAILED to back up ${DB} (the rename of its verified archive failed)" >&2
            FAILED="$FAILED $DB"
        fi
    else
        rm -f "$OUT.partial"; OUT=""
        [ "$dump_rc" -eq 143 ] || [ "$verify_rc" -eq 143 ] || log "FAILED to back up ${DB} (pg_dump or the archive check failed)" >&2
        FAILED="$FAILED $DB"
    fi
done


# One summary line per run — what install-backup-agent.sh reads back when
# it cannot see the destination itself.
if [ -n "$FAILED" ]; then
    log "run incomplete; failed:${FAILED}" >&2
    exit 1
fi
log "run complete: $STORES store(s)"
