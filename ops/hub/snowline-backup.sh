#!/bin/bash
# Hourly dump of EVERY live Snowline Postgres store on the hub. Launched by
# ops/hub/launchagents/dev.snowline.backup.plist (the owner's per-user
# LaunchAgent, installed by ops/hub/install-daemons.sh); see
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
. "$(dirname "$0")/launchdaemons/lib.sh" || { echo "$(date -u +%FT%TZ) cannot source $(dirname "$0")/launchdaemons/lib.sh; is this a complete checkout?" >&2; exit 2; }
# Operator overrides live in ~/.config/snowline/backup.env (optional; sourced
# here), NOT in the launchd plist: install-daemons.sh replaces the plist with
# the tracked copy whenever it differs, so anything put there would be lost.
BACKUP_ENV="$HOME/.config/snowline/backup.env"
if [ -f "$BACKUP_ENV" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$BACKUP_ENV"
    set +a
fi
# Off-machine by default: iCloud Drive replicates the dumps off this host, so a
# disk failure / theft doesn't take the backups with it. The default lives in
# lib.sh (BACKUP_DIR) so install-daemons.sh judges the same folder. Override
# with SNOWLINE_BACKUP_DIR (in backup.env) to point at a tailnet rsync
# target, an external volume, etc.
DEST="${SNOWLINE_BACKUP_DIR:-$BACKUP_DIR}"
RETENTION_DAYS="${SNOWLINE_BACKUP_RETENTION_DAYS:-30}"
case "$RETENTION_DAYS" in ''|*[!0-9]*|0) log "SNOWLINE_BACKUP_RETENTION_DAYS must be a whole number of days, at least 1: '$RETENTION_DAYS'" >&2; exit 2 ;; esac
# name_stamp <path> — the YYYYMMDD-HHMMSS UTC stamp of a Z-named dump or
# placeholder (…-YYYYMMDD-HHMMSSZ.dump[.icloud]); fails for any other name.
# Parameter expansion only: it runs once per file in --check and prune.
name_stamp() {
    ns="${1##*/}"; ns="${ns%.icloud}"; ns="${ns%Z.dump}"
    ns="${ns#"${ns%???????????????}"}"   # last 15 chars
    case "$ns" in
        [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) printf '%s\n' "$ns" ;;
        *) return 1 ;;
    esac
}
# z_dumps <name-prefix> — one "<stamp>\t<path>" line per Z-named dump or
# placeholder of that prefix in DEST (`*` for every store). The ONE rule
# --check (evidence) and the prune (retention) share. find's stderr goes
# to Z_DUMPS_ERR for the caller to inspect.
Z_DUMPS_ERR="$(mktemp -t snowline-backup-find)"
z_dumps() {
    find "$DEST" -maxdepth 1 \( -name "$1-[0-9]*Z.dump" -o -name ".$1-[0-9]*Z.dump.icloud" \) -type f 2>"$Z_DUMPS_ERR" \
    | while IFS= read -r zf; do
        zs="$(name_stamp "$zf")" || continue
        printf '%s\t%s\n' "$zs" "$zf"
    done
}

# Queries install-daemons.sh makes by running this script AS THE OWNER
# (so backup.env, TCC consent and the destination are exactly this
# script's), checked before anything below touches the positional params:
#   --print-dest        print the resolved destination
#   --check <minutes>   exit 0 if a dump (or its iCloud-evicted placeholder)
#                       newer than <minutes> exists in the destination,
#                       1 if none, 2 if the destination cannot be read
case "${1:-}" in
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
        mins="${2:-}"
        case "$mins" in ''|*[!0-9]*) echo "--check needs a number of minutes" >&2; exit 2 ;; esac
        [ -d "$DEST" ] || { echo NONE; exit 1; }
        cutoff="$(date -u -v-"${mins}"M +%Y%m%d-%H%M%S 2>/dev/null)" || { echo UNREADABLE; exit 2; }
        [ -n "$cutoff" ] || { echo UNREADABLE; exit 2; }
        found=no
        while IFS="$(printf '\t')" read -r s _; do
            [ "$s" \< "$cutoff" ] || found=yes
        done <<EOF_Z
$(z_dumps '*')
EOF_Z
        if [ -s "$Z_DUMPS_ERR" ]; then rm -f "$Z_DUMPS_ERR"; echo UNREADABLE; exit 2; fi
        rm -f "$Z_DUMPS_ERR"
        [ "$found" = yes ] && { echo OK; exit 0; }
        echo NONE; exit 1
        ;;
    "") ;;   # no arguments: a real run
    *) log "unknown argument '$1' (this script takes no arguments except --print-dest and --check <minutes>); refusing to run" >&2; exit 2 ;;
esac
# An explicit store list is validated here, before anything slow or
# side-effecting: a whitespace-only value is "unset", not "dump nothing";
# the split is done with globbing off so a stray `*` cannot expand against
# the cwd, and each name must be a plain identifier (valid_names, below,
# is the one rule for every source of names).
# valid_names <mode:strict|skip> <names...> — prints the identifiers; in
# strict mode a bad name is fatal (exit 2), in skip mode it is logged and
# dropped.
valid_names() {
    vn_mode="$1"; shift
    for vn in "$@"; do
        case "$vn" in
            *[!A-Za-z0-9_]*)
                if [ "$vn_mode" = strict ]; then
                    log "SNOWLINE_BACKUP_DBS entry is not a database name: '$vn'" >&2; exit 2
                fi
                log "WARNING: skipping database with a non-identifier name: '$vn'" >&2 ;;
            *) printf '%s ' "$vn" ;;
        esac
    done
}
DBS_EXPLICIT="${SNOWLINE_BACKUP_DBS:-}"
set -f
# shellcheck disable=SC2086
set -- $DBS_EXPLICIT
set +f
# (valid_names runs in a subshell here, so its exit 2 must be propagated)
DBS_EXPLICIT="$(valid_names strict "$@")" || exit 2
DBS_EXPLICIT="${DBS_EXPLICIT% }"
# Exit codes: 2 = cannot run at all (destination / iCloud root / unwritable /
# lock — the environment, not the data); 1 = some store(s) failed to dump;
# 3 = another run held the lock (stepped aside). install-daemons.sh reads
# them to judge the RunAtLoad run; nothing is rolled back on any of them.
die() { log "$*" >&2; exit 2; }

# --- run lock: a flock on $BACKUP_LOCK, held for the whole run ---
# fd 9 is opened on the lock file for the life of this process; a one-line
# python takes the flock on that inherited fd (the lock lives on the open
# file description, so it stays held after python exits, for as long as
# this script keeps fd 9). The kernel drops it on exit or crash: no stale
# lock, no pid to trust. lib.sh's unit_busy_wait probes the same file before
# the ops scripts boot this unit out (a probe holds a shared lock for an
# instant, so the acquire retries over a few seconds). A second run while
# one holds the lock (RunAtLoad racing the hourly fire) steps aside with
# exit 3, which install-daemons.sh never mistakes for "dumps landed"; any
# other flock error (an fs without flock, a bad fd) is exit 2.
mkdir -p "$(dirname "$BACKUP_LOCK")"
exec 9>>"$BACKUP_LOCK" || die "cannot open the run lock $BACKUP_LOCK (root-owned from a sudo run?); fix its ownership"
# /usr/bin/perl ships with base macOS (the python3 there is a Command Line
# Tools stub); it flocks the inherited fd 9 in place, so the lock outlives it.
/usr/bin/perl -e 'use Fcntl qw(:flock);
    open(my $fh, ">>&=", 9) or exit 2;
    for (1..5) {
        exit 0 if flock($fh, LOCK_EX | LOCK_NB);
        exit 2 unless $!{EWOULDBLOCK} || $!{EAGAIN};
        sleep 1;   # a lock_state probe holds it for an instant; a real run for minutes
    }
    exit 3;'
case $? in
    0) ;;
    3) log "another backup run holds $BACKUP_LOCK; stepping aside"; exit 3 ;;
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
# Under a verified iCloud root the leaf folders may be created freely; any
# other destination (external volume, rsync target) must already EXIST and
# is never created here — a destination that is itself a mount point
# (/Volumes/Backups) would otherwise be silently recreated as a plain local
# directory while the disk is unplugged.
case "$DEST" in
    "$ICLOUD_ROOT"/*)
        [ -d "$ICLOUD_ROOT" ] || die "iCloud Drive root missing ($ICLOUD_ROOT); refusing to write local-only backups — is iCloud Drive set up for this user?"
        mkdir -p "$DEST" || die "cannot create backup destination: $DEST"
        ;;
    *)
        [ -d "$DEST" ] || die "backup destination missing: $DEST (not mounted? it is never created by this script)"
        ;;
esac
# A real create, not `[ -w ]`: access(2) does not see a TCC denial (a
# system-domain process without iCloud Drive consent) or a full/read-only
# volume, and those must be exit 2 ("cannot run here"), not a per-store 1.
PROBE="$DEST/.probe.$$"
( : > "$PROBE" ) 2>/dev/null || die "backup destination not writable: $DEST (TCC consent? full or read-only volume?)"
rm -f "$PROBE"
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
CATALOG_ERR="$(mktemp -t snowline-backup-psql)"
# Only databases this role can actually connect to: a parked store
# (datallowconn = false) or one without CONNECT would otherwise fail every
# hourly run with exit 1 until someone hand-writes SNOWLINE_BACKUP_DBS.
CATALOG="$(psql -X -At -d postgres -c "select datname from pg_database where not datistemplate and datname <> 'postgres' and datallowconn and has_database_privilege(datname, 'CONNECT') order by 1" 2>"$CATALOG_ERR")" || CATALOG_OK=no
LIVE="$(printf '%s\n' "$CATALOG" | grep -E "$STORE_RE" | grep -v -E "$SCRATCH_RE" | grep -v '^$' || true)"
# Catalog names go through the same identifier rule (skip-and-warn): a
# quoted name with a space would otherwise split into two bogus stores.
# Split on newlines with globbing off; LIVE is one name per line.
set -f
IFS_SAVE="$IFS"; IFS='
'
# shellcheck disable=SC2086
set -- $LIVE
IFS="$IFS_SAVE"
set +f
LIVE="$(valid_names skip "$@")"
LIVE="${LIVE% }"
if [ -n "$DBS_EXPLICIT" ]; then
    DBS="$DBS_EXPLICIT"
    EXCLUDED=""
    for db in $LIVE; do
        case " $DBS " in
            *" $db "*) ;;
            *) EXCLUDED="$EXCLUDED $db" ;;
        esac
    done
    [ -z "$EXCLUDED" ] || log "note: SNOWLINE_BACKUP_DBS excludes live store(s):${EXCLUDED}" >&2
elif [ "$CATALOG_OK" = yes ] && [ -n "$LIVE" ]; then
    DBS="$LIVE"
elif [ "$CATALOG_OK" = yes ]; then
    # Readable catalog with no usable snowline_* store: the wrong cluster
    # (PGHOST / PGPORT leaking in, a fresh cluster before migration), or
    # every name was filtered. Never exit 0 with nothing dumped: try the
    # known stores, which then fail loudly (exit 1).
    DBS="$FALLBACK_DBS"
    log "WARNING: the catalog holds no usable snowline_* store (wrong cluster?); trying the fallback list: $DBS" >&2
else
    DBS="$FALLBACK_DBS"
    log "WARNING: could not list databases from the server ($(tr '\n' ' ' < "$CATALOG_ERR")); dumping the fallback list only: $DBS" >&2
fi
rm -f "$CATALOG_ERR"

# UTC stamp so file names sort monotonically (no DST fold) and match the log.
STAMP="$(date -u +%Y%m%d-%H%M%SZ)"
FAILED=""

# Word-split DBS once, with globbing off (names are validated above or
# catalog-sourced). A SIGTERM mid-dump (the unit booted out, a logout, a
# shutdown) logs itself and removes the partial it was writing, so the log
# shows a kill rather than a hang.
set -f
# shellcheck disable=SC2086
set -- $DBS
set +f
# prune_by_name <store> — delete this store's Z-stamped dumps and
# placeholders whose name stamp is older than RETENTION_DAYS (string
# comparison against one cutoff stamp; no fork per file).
PRUNE_CUTOFF="$(date -u -v-"${RETENTION_DAYS}"d +%Y%m%d-%H%M%S 2>/dev/null)" || PRUNE_CUTOFF=""
prune_by_name() {
    [ -n "$PRUNE_CUTOFF" ] || return 0   # no cutoff, no pruning (never over-delete)
    z_dumps "$1" | while IFS="$(printf '\t')" read -r pb_stamp pb_f; do
        [ "$pb_stamp" \< "$PRUNE_CUTOFF" ] && rm -f "$pb_f"
    done
    return 0
}
OUT=""; DUMP_PID=""
# bash runs a trap only once the foreground command returns, so pg_dump is
# started in the background and waited for: `wait` is interruptible, the
# handler runs at once, kills the dump and removes its partial.
trap 'log "interrupted (SIGTERM/SIGINT); removing ${OUT:+$OUT.partial}" >&2; [ -z "$DUMP_PID" ] || kill "$DUMP_PID" 2>/dev/null; [ -z "$OUT" ] || rm -f "$OUT.partial"; exit 143' TERM INT
for DB in "$@"; do
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
        && mv "$OUT.partial" "$OUT"; then
        # Size is best-effort (an iCloud file can be evicted to a placeholder
        # the instant it lands, making du error).
        SIZE="$(du -h "$OUT" 2>/dev/null | cut -f1 || true)"
        log "backed up ${DB} -> ${OUT} (${SIZE:-size unknown})"
    else
        rm -f "$OUT.partial"
        log "FAILED to back up ${DB} (pg_dump, archive check, or rename failed)" >&2
        FAILED="$FAILED $DB"
        continue
    fi

    # Prune this store's dumps past the retention window. Per-store pattern, so
    # dumps of stores no longer in the list (e.g. the legacy snowline_dev
    # history) are left alone. iCloud evicts old files to `.NAME.icloud`
    # placeholders, which must be pruned by the same rule or the folder grows
    # without bound. Age comes from the UTC stamp in the NAME (same rule as
    # --check: a placeholder's mtime is eviction time, not dump time); the
    # old pre-Z names are local time and go by mtime. Non-fatal: a find
    # hiccup must not make a run whose dump already succeeded read as failed.
    prune_by_name "$DB" || true
    # legacy pre-Z names (local-time stamps) and their placeholders: by mtime
    find "$DEST" -maxdepth 1 \( -name "${DB}-*[0-9].dump" -o -name ".${DB}-*[0-9].dump.icloud" \) -type f -mtime +"$RETENTION_DAYS" -delete 2>/dev/null || true
done
trap - TERM INT
rm -f "$Z_DUMPS_ERR"

# Stale .partial leftovers from any store.
find "$DEST" -maxdepth 1 -name '*.partial' -type f -mtime +1 -delete || true

if [ -n "$FAILED" ]; then
    log "run incomplete; failed:${FAILED}" >&2
    exit 1
fi
