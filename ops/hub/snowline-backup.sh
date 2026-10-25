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
ARG1="${1:-}"; ARG2="${2:-}"
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
# Temp files for the catalog read's and the dump listing's stderr: created
# here, in the parent shell (z_dumps runs in subshells, where an assignment
# would never reach the trap), removed on every exit path.
# Every run ends with ONE machine-readable line, "run exit <code>", written
# by the EXIT trap whatever the path (success, a refusal, a step-aside, an
# interrupt — armed before anything below can fail): install-backup-agent.sh
# reads the LAST such line to judge a run when it cannot see the
# destination itself. Queries (--print-dest, --check) do not log one
# (QUIET_EXIT, decided with the command line above).
CATALOG_ERR=""; Z_DUMPS_ERR=""; PROBE=""   # temp files, set below; removed on every exit
on_exit() {
    oe_rc=$?
    rm -f ${CATALOG_ERR:+"$CATALOG_ERR"} ${Z_DUMPS_ERR:+"$Z_DUMPS_ERR"} ${PROBE:+"$PROBE"}
    [ "$QUIET_EXIT" = yes ] || log "run exit $oe_rc"
}
trap on_exit EXIT
[ "$QUIET_EXIT" = yes ] || log "run start"   # the run's real start, for the installer
CATALOG_ERR="$(mktemp -t snowline-backup-psql)" || die "cannot create a temp file (TMPDIR unwritable?)"
Z_DUMPS_ERR="$(mktemp -t snowline-backup-find)" || die "cannot create a temp file (TMPDIR unwritable?)"
# A SIGTERM/SIGINT at ANY point (a bootout during the Postgres wait, a
# logout, a shutdown) logs itself; once a dump is in flight (OUT/DUMP_PID
# set below) it is killed and its partial removed. Armed here, not just
# around the dump loop, so a run never vanishes without a log line.
OUT=""; DUMP_PID=""
trap 'log "interrupted (SIGTERM/SIGINT)${OUT:+; removing $OUT.partial}" >&2; [ -z "$DUMP_PID" ] || kill "$DUMP_PID" 2>/dev/null; [ -z "$OUT" ] || rm -f "$OUT.partial"; exit 143' TERM INT
# Operator overrides live in ~/.config/snowline/backup.env (optional; sourced
# here), NOT in the launchd plist: install-backup-agent.sh replaces the plist with
# the tracked copy whenever it differs, so anything put there would be lost.
# The file supplies DEFAULTS: a variable already set in the environment
# (a scratch test's SNOWLINE_BACKUP_DIR=/tmp/x, say) wins over it.
BACKUP_ENV="$HOME/.config/snowline/backup.env"
if [ -f "$BACKUP_ENV" ]; then
    env_dir="${SNOWLINE_BACKUP_DIR:-}"; env_days="${SNOWLINE_BACKUP_RETENTION_DAYS:-}"; env_dbs="${SNOWLINE_BACKUP_DBS:-}"
    # (plain `.`, not `set -a`: nothing in the file is for a child process,
    # and an exported PGHOST or PATH there would silently retarget psql)
    set +u   # the file may reference variables this shell does not have
    # shellcheck disable=SC1090
    . "$BACKUP_ENV" || die "cannot source $BACKUP_ENV (syntax error?); refusing to run half-configured"
    set -u
    # (and say so when the file's value lost: the hub's pre-#319 agent plist
    # exported SNOWLINE_BACKUP_DIR itself until install-backup-agent.sh
    # replaced it, which made a backup.env value look ignored)
    shadow() { [ -z "$2" ] || [ "$2" = "$3" ] || log "note: $1 from the environment ($2) overrides $BACKUP_ENV ($3)" >&2; }
    shadow SNOWLINE_BACKUP_DIR "$env_dir" "${SNOWLINE_BACKUP_DIR:-}"
    shadow SNOWLINE_BACKUP_RETENTION_DAYS "$env_days" "${SNOWLINE_BACKUP_RETENTION_DAYS:-}"
    shadow SNOWLINE_BACKUP_DBS "$env_dbs" "${SNOWLINE_BACKUP_DBS:-}"
    [ -z "$env_dir" ]  || SNOWLINE_BACKUP_DIR="$env_dir"
    [ -z "$env_days" ] || SNOWLINE_BACKUP_RETENTION_DAYS="$env_days"
    [ -z "$env_dbs" ]  || SNOWLINE_BACKUP_DBS="$env_dbs"
fi
# Off-machine by default: iCloud Drive replicates the dumps off this host, so a
# disk failure / theft doesn't take the backups with it. The default lives in
# lib.sh (BACKUP_DIR) so install-backup-agent.sh judges the same folder. Override
# with SNOWLINE_BACKUP_DIR (in backup.env) to point at a tailnet rsync
# target, an external volume, etc.
DEST="${SNOWLINE_BACKUP_DIR:-$BACKUP_DIR}"
RETENTION_DAYS="${SNOWLINE_BACKUP_RETENTION_DAYS:-30}"
case "$RETENTION_DAYS" in ''|*[!0-9]*) die "SNOWLINE_BACKUP_RETENTION_DAYS must be a whole number of days, at least 1: '$RETENTION_DAYS'" ;; esac
[ "$RETENTION_DAYS" -ge 1 ] || die "SNOWLINE_BACKUP_RETENTION_DAYS must be at least 1: '$RETENTION_DAYS'"
# parse_dump_name <path> — the ONE grammar of this script's file names,
# <store>-YYYYMMDD-HHMMSS[Z].dump, optionally iCloud-evicted to
# .<name>.icloud: sets NAME_STORE and NAME_STAMP, or fails for any other
# name. (The pre-#319 names lack the Z and are local time — a few hours
# off, which the 30-day retention does not care about.) A bash regex: no
# fork per file.
DUMP_NAME_RE='^(.*/)?\.?([A-Za-z0-9_]+)-([0-9]{8}-[0-9]{6})Z?\.dump(\.icloud)?$'
parse_dump_name() {
    [[ $1 =~ $DUMP_NAME_RE ]] || return 1
    NAME_STORE="${BASH_REMATCH[2]}"; NAME_STAMP="${BASH_REMATCH[3]}"
}
# z_dumps <name-prefix> [all] — one "<store>\t<stamp>\t<path>" line per dump
# or placeholder of that prefix in DEST (`*` for every store): Z-named
# (UTC) ones by default, pre-#319 local-time names too with `all`. The ONE
# listing --check (evidence: Z names only, written by this script) and the
# prune (retention: every name) share. find's stderr goes to Z_DUMPS_ERR
# for the caller to inspect.
TAB=$'\t'
z_dumps() {
    if [ "${2:-}" = all ]; then zd_tail=''; else zd_tail='Z'; fi   # all: Z names and pre-#319 names alike
    find "$DEST" -maxdepth 1 \( -name "$1-[0-9]*${zd_tail}.dump" -o -name ".$1-[0-9]*${zd_tail}.dump.icloud" \) -type f 2>"$Z_DUMPS_ERR" \
    | while IFS= read -r zf; do
        parse_dump_name "$zf" || continue
        printf '%s\t%s\t%s\n' "$NAME_STORE" "$NAME_STAMP" "$zf"
    done
}

# has_word <word> <list...> — is <word> one of the space-separated words?
has_word() { hw="$1"; shift; case " $* " in *" $hw "*) return 0 ;; esac; return 1; }

# SNOWLINE_BACKUP_DBS, normalised ONCE (split on any whitespace with
# globbing off, re-joined with single spaces; the positional parameters are
# never touched) so --check and the run agree on membership; validated just
# below, before anything side-effecting.
DBS_EXPLICIT=""
set -f
for vn in ${SNOWLINE_BACKUP_DBS:-}; do DBS_EXPLICIT="$DBS_EXPLICIT $vn"; done
set +f
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

# dest_state — the ONE destination rule, for --check and the run alike:
# prints ok (exists), no-leaf (absent, but under a present iCloud root: may
# be created), or missing (iCloud root absent, or a non-iCloud destination
# that is not there — never created here: a destination that is itself a
# mount point would otherwise be silently recreated as a plain local
# directory while the disk is unplugged).
dest_state() {
    if [ -d "$DEST" ]; then echo ok; return; fi
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
        mins="$ARG2"
        case "$mins" in ''|*[!0-9]*) echo "--check needs a number of minutes" >&2; exit 2 ;; esac
        # Same destination rule as a run (dest_state): a leaf that does not
        # exist yet under a present iCloud root is "no dump yet" (NONE); a
        # missing root or non-iCloud destination is "cannot run here".
        case "$(dest_state)" in
            ok) ;;
            no-leaf) echo NONE; exit 1 ;;
            *) echo MISSING; exit 2 ;;   # a run would exit 2 too: nothing to read, not "unreadable from here"
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
$(z_dumps '*')
EOF_Z
        if [ -s "$Z_DUMPS_ERR" ]; then echo UNREADABLE; exit 2; fi
        [ "$found" = yes ] && { echo OK; exit 0; }
        echo NONE; exit 1
        ;;
    "") ;;   # no arguments: a real run
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
mkdir -p "$(dirname "$BACKUP_LOCK")"
exec 9>>"$BACKUP_LOCK" || die "cannot open the run lock $BACKUP_LOCK (root-owned from a sudo run?); fix its ownership"
# flock_fd (lib.sh) flocks the inherited fd 9 in place, so the lock
# outlives the helper; five tries a second apart, because a lock_state probe
# holds the file for an instant while a real run holds it for minutes.
flock_fd 9 ex 5
case $? in
    0) ;;
    1) log "another backup run holds $BACKUP_LOCK; stepping aside"; exit 3 ;;
    127) log "WARNING: /usr/bin/perl is missing; running WITHOUT the run lock (the dump is the point, the lock a courtesy)" >&2 ;;
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
    ok) ;;
    no-leaf) mkdir -p "$DEST" || die "cannot create backup destination: $DEST" ;;
    *) die "backup destination missing: $DEST (iCloud Drive root absent, or a volume not mounted — it is never created by this script)" ;;
esac
# A real create, not `[ -w ]`: access(2) does not see a TCC denial (a
# system-domain process without iCloud Drive consent) or a full/read-only
# volume, and those must be exit 2 ("cannot run here"), not a per-store 1.
PROBE="$DEST/.probe.$$"
( : > "$PROBE" ) 2>/dev/null || die "backup destination not writable: $DEST (TCC consent? full or read-only volume?)"
rm -f "$PROBE"; PROBE=""
# Owner-only posture for the folder and for every dump in it: new dumps are
# written under umask 077; older ones (0644 before this script enforced it)
# and dumps that iCloud re-materialises are tightened each run — a few
# hundred stats an hour. Best effort on an iCloud folder, but say so.
chmod 700 "$DEST" 2>/dev/null || log "WARNING: could not chmod 700 $DEST" >&2
# (`-exec … {} +` runs nothing when nothing matches: one walk, no chmod
# unless a loose file exists)
find "$DEST" -maxdepth 1 -name '*.dump' -type f -perm +044 -exec chmod 600 {} + 2>/dev/null \
    || log "WARNING: could not tighten permissions on existing dumps in $DEST" >&2

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
CATALOG_ROWS="$(psql -X -At -d postgres -c "select datname, (datallowconn and has_database_privilege(datname, 'CONNECT')) from pg_database where not datistemplate and datname <> 'postgres' and datname ~ '^[A-Za-z0-9_]+\$' order by 1" 2>"$CATALOG_ERR")" || CATALOG_OK=no
# One pass over the rows: the store rule (store_wanted) applied once per
# name, connectable names to LIVE, the others to UNCONNECTABLE.
LIVE=""; UNCONNECTABLE=""
while IFS='|' read -r cat_name cat_ok; do
    [ -n "$cat_name" ] || continue
    store_wanted "$cat_name" || continue
    if [ "$cat_ok" = t ]; then LIVE="$LIVE $cat_name"; else UNCONNECTABLE="$UNCONNECTABLE $cat_name"; fi
done <<EOF_ROWS
$CATALOG_ROWS
EOF_ROWS
LIVE="${LIVE# }"; UNCONNECTABLE="${UNCONNECTABLE# }"
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
    z_dumps '*' all | sort -t "$TAB" -k1,1 -k2,2r \
    | awk -F"$TAB" -v cutoff="$PRUNE_CUTOFF" -v keep="$KEEP_MIN" '
        $1 == "snowline_dev" { next }
        $1 != store { store = $1; n = 0 }
        { n++; if (n > keep && $2 < cutoff) print $3 }' \
    | while IFS= read -r pa_f; do rm -f "$pa_f"; done
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
prune_all || true
set -f
for DB in $DBS; do
    set +f
    STORES=$((STORES + 1))
    OUT="$DEST/${DB}-${STAMP}.dump"
    # Dump to a .partial name, verify the archive is readable, then atomically
    # rename — an interrupted or truncated dump (machine sleep, OOM, iCloud
    # hiccup) never leaves a file that looks like a complete backup. One store
    # failing must not skip the others: record it and keep going.
    # --lock-wait-timeout: a dump wants ACCESS SHARE on every table; a service
    # mid-migration (ACCESS EXCLUSIVE) must not be blocked by it for long —
    # better a store failure this hour than a stalled migration.
    pg_dump -Fc --lock-wait-timeout=60000 "$DB" > "$OUT.partial" &
    DUMP_PID=$!
    wait "$DUMP_PID"; dump_rc=$?
    DUMP_PID=""
    if [ "$dump_rc" -eq 0 ] \
        && pg_restore -l "$OUT.partial" > /dev/null \
        && mv "$OUT.partial" "$OUT" && { LANDED="$OUT"; OUT=""; }; then
        # (OUT is cleared in the same breath as the rename: a signal between
        # the two must not make the trap report a landed dump as removed.)
        # Size is best-effort (an iCloud file can be evicted to a placeholder
        # the instant it lands, making du error).
        SIZE="$(du -h "$LANDED" 2>/dev/null | awk '{ print $1 }' || true)"
        log "backed up ${DB} -> ${LANDED} (${SIZE:-size unknown})"
    else
        rm -f "$OUT.partial"; OUT=""
        log "FAILED to back up ${DB} (pg_dump, archive check, or rename failed)" >&2
        FAILED="$FAILED $DB"
    fi
    set -f
done
set +f


# Stale .partial leftovers from any store.
find "$DEST" -maxdepth 1 -name '*.partial' -type f -mtime +1 -delete || true

# One summary line per run — what install-backup-agent.sh reads back when
# it cannot see the destination itself.
if [ -n "$FAILED" ]; then
    log "run incomplete; failed:${FAILED}" >&2
    exit 1
fi
log "run complete: $STORES store(s)"
