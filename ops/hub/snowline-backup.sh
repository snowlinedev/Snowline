#!/bin/bash
# Hourly dump of EVERY live Snowline Postgres store on the hub. Launched by
# ops/hub/launchdaemons/dev.snowline.backup.plist (a system LaunchDaemon
# installed by ops/hub/install-daemons.sh, running as the owner); see
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
DBS_EXPLICIT="${SNOWLINE_BACKUP_DBS:-}"
FALLBACK_DBS="snowline_platform snowline_pm snowline_governance snowline_memory snowline_musher snowline_marketing"
# Only Snowline's own stores (`snowline_*`) — the hub's cluster may host other
# projects' databases, which are not this job's to dump — minus the scratch
# patterns: test / dry-run / e2e stores, the legacy snowline_dev, and
# `*_restore` (the documented inspect-first restore target).
STORE_RE='^snowline_'
SCRATCH_RE='(_test|_dryrun|_e2e|_restore|^snowline_dev$)'
ICLOUD_ROOT="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
# Off-machine by default: iCloud Drive replicates the dumps off this host, so a
# disk failure / theft doesn't take the backups with it. Override with the
# SNOWLINE_BACKUP_DIR env var in the plist to point at a tailnet rsync target,
# an external volume, etc.
DEST="${SNOWLINE_BACKUP_DIR:-$ICLOUD_ROOT/Snowline/db-backups}"
RETENTION_DAYS="${SNOWLINE_BACKUP_RETENTION_DAYS:-30}"

now() { date -u +%FT%TZ; }
# Exit codes: 2 = cannot run at all (destination / iCloud root / unwritable —
# the environment, not the data); 1 = some store(s) failed to dump.
# install-daemons.sh rolls the unit back to the per-user agent only on 2.
die() { echo "$(now) $*" >&2; exit 2; }

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
[ -w "$DEST" ] || die "backup destination not writable: $DEST"
# Owner-only posture for the folder AND for dumps written before this script
# enforced it (they were 0644). Best effort on an iCloud folder, but say so.
chmod 700 "$DEST" 2>/dev/null || echo "$(now) WARNING: could not chmod 700 $DEST" >&2
find "$DEST" -maxdepth 1 -name '*.dump' -type f -perm +044 -exec chmod 600 {} + 2>/dev/null \
    || echo "$(now) WARNING: could not tighten permissions on existing dumps in $DEST" >&2

# --- wait for Postgres (bounded; see lib.sh) ---
# shellcheck disable=SC1091
. "$(dirname "$0")/launchdaemons/lib.sh"
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
    [ -z "$EXCLUDED" ] || echo "$(now) note: SNOWLINE_BACKUP_DBS excludes live store(s):${EXCLUDED}" >&2
elif [ "$CATALOG_OK" = yes ]; then
    DBS="$(printf '%s\n' "$LIVE" | tr '\n' ' ')"
    [ -n "$LIVE" ] || echo "$(now) WARNING: the catalog holds no non-scratch database; nothing to dump" >&2
else
    DBS="$FALLBACK_DBS"
    echo "$(now) WARNING: could not list databases from the server ($(tr '\n' ' ' < "$CATALOG_ERR")); dumping the fallback list only: $DBS" >&2
fi
rm -f "$CATALOG_ERR"

# UTC stamp so file names sort monotonically (no DST fold) and match the log.
STAMP="$(date -u +%Y%m%d-%H%M%SZ)"
FAILED=""

for DB in $DBS; do
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
        echo "$(now) backed up ${DB} -> ${OUT} (${SIZE:-size unknown})"
    else
        rm -f "$OUT.partial"
        echo "$(now) FAILED to back up ${DB} (pg_dump, archive check, or rename failed)" >&2
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

# Stale .partial leftovers from any store.
find "$DEST" -maxdepth 1 -name '*.partial' -type f -mtime +1 -delete || true

if [ -n "$FAILED" ]; then
    echo "$(now) run incomplete; failed:${FAILED}" >&2
    exit 1
fi
