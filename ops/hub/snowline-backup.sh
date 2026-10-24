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
# An explicit store list is validated here, before anything slow or
# side-effecting: a whitespace-only value is "unset", not "dump nothing";
# the split is done with globbing off so a stray `*` cannot expand against
# the cwd, and each name must be a plain identifier.
DBS_EXPLICIT="${SNOWLINE_BACKUP_DBS:-}"
set -f; set -- $DBS_EXPLICIT; set +f
DBS_EXPLICIT="$*"
for db in $DBS_EXPLICIT; do
    case "$db" in
        *[!A-Za-z0-9_]*) log "SNOWLINE_BACKUP_DBS entry is not a database name: '$db'" >&2; exit 2 ;;
    esac
done
# `--print-dest`: print the resolved destination and exit — how
# install-daemons.sh learns where to look for a fresh dump, by asking this
# script as the owner instead of re-reading backup.env as root.
if [ "${1:-}" = --print-dest ]; then
    printf '%s\n' "$DEST"
    exit 0
fi
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
/usr/bin/python3 -c '
import errno, fcntl, sys, time
for attempt in range(5):
    try:
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except OSError as e:
        if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
            print("flock failed on the run lock: %s" % e, file=sys.stderr)
            sys.exit(2)
        time.sleep(1)   # a lock_held probe holds it for an instant; a real run for minutes
sys.exit(3)
'
case $? in
    0) ;;
    3) log "another backup run holds $BACKUP_LOCK; stepping aside"; exit 3 ;;
    *) die "cannot take the run lock $BACKUP_LOCK" ;;
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
# Under a verified iCloud root the leaf folders may be created freely; for
# any other destination (external volume, rsync target) the parent must
# already exist, or an unmounted volume would be silently replaced by a
# local directory.
case "$DEST" in
    "$ICLOUD_ROOT"/*)
        [ -d "$ICLOUD_ROOT" ] || die "iCloud Drive root missing ($ICLOUD_ROOT); refusing to write local-only backups — is iCloud Drive set up for this user?"
        ;;
    *)
        [ -d "$(dirname "$DEST")" ] || die "backup destination parent missing: $(dirname "$DEST")"
        ;;
esac
mkdir -p "$DEST" || die "cannot create backup destination: $DEST"
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
CATALOG="$(psql -X -At -d postgres -c "select datname from pg_database where not datistemplate and datname <> 'postgres' order by 1" 2>"$CATALOG_ERR")" || CATALOG_OK=no
LIVE="$(printf '%s\n' "$CATALOG" | grep -E "$STORE_RE" | grep -v -E "$SCRATCH_RE" | grep -v '^$' || true)"
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
    DBS="$(printf '%s\n' "$LIVE" | tr '\n' ' ')"
elif [ "$CATALOG_OK" = yes ]; then
    # Readable catalog with no snowline_* store: the wrong cluster (PGHOST /
    # PGPORT leaking in, a fresh cluster before migration). Never exit 0 with
    # nothing dumped: try the known stores, which then fail loudly (exit 1).
    DBS="$FALLBACK_DBS"
    log "WARNING: the catalog holds no snowline_* store (wrong cluster?); trying the fallback list: $DBS" >&2
else
    DBS="$FALLBACK_DBS"
    log "WARNING: could not list databases from the server ($(tr '\n' ' ' < "$CATALOG_ERR")); dumping the fallback list only: $DBS" >&2
fi
rm -f "$CATALOG_ERR"

# UTC stamp so file names sort monotonically (no DST fold) and match the log.
STAMP="$(date -u +%Y%m%d-%H%M%SZ)"
FAILED=""

set -f   # word-split DBS with globbing off (names are validated above / catalog-sourced)
for DB in $DBS; do
    set +f
    OUT="$DEST/${DB}-${STAMP}.dump"
    # Dump to a .partial name, verify the archive is readable, then atomically
    # rename — an interrupted or truncated dump (machine sleep, OOM, iCloud
    # hiccup) never leaves a file that looks like a complete backup. One store
    # failing must not skip the others: record it and keep going.
    if pg_dump -Fc "$DB" > "$OUT.partial" \
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
    # without bound. Non-fatal: a malformed RETENTION_DAYS or a find hiccup
    # must not make a run whose dump already succeeded read as failed.
    find "$DEST" -maxdepth 1 -name "${DB}-*.dump" -type f -mtime +"$RETENTION_DAYS" -delete || true
    find "$DEST" -maxdepth 1 -name ".${DB}-*.dump.icloud" -type f -mtime +"$RETENTION_DAYS" -delete || true
done
set +f

# Stale .partial leftovers from any store.
find "$DEST" -maxdepth 1 -name '*.partial' -type f -mtime +1 -delete || true

if [ -n "$FAILED" ]; then
    log "run incomplete; failed:${FAILED}" >&2
    exit 1
fi
