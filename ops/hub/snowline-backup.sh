#!/bin/bash
# Launched by ops/hub/dev.snowline.backup.plist. Dumps EVERY live Snowline
# Postgres store (pg_dump custom format, compressed) into $SNOWLINE_BACKUP_DIR,
# then prunes each store's dumps older than the retention window. One-shot:
# launchd's timer fires it on schedule, so a failed run is simply a skipped run
# (no retry storm) — the next scheduled run takes a fresh dump.
#
# Since the plugin split there is one database per service (platform, pm,
# governance, memory) — together they are the single source of truth for the
# whole Snowline layer (scopes, milestones, work items, decisions, artifacts,
# working memory). None of it is in git — these dumps are the only way back
# from disk loss / a bad migration. Decision e4578136 (hourly, off-machine,
# secrets NOT backed up) still governs; this script just widens it to all
# four stores.
#
# Restore one store (to the live DB, destructive):
#   pg_restore --clean --if-exists --no-owner -d snowline_pm <dump>
# Restore (to a scratch DB, to inspect first):
#   createdb snowline_restore && pg_restore --no-owner -d snowline_restore <dump>
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Space-separated list of databases to dump. Must match the *_DATABASE_URL
# targets in ~/.config/snowline/{platform,pm,governance,memory}.env.
DBS="${SNOWLINE_BACKUP_DBS:-snowline_platform snowline_pm snowline_governance snowline_memory}"
# Off-machine by default: iCloud Drive replicates the dumps off this host, so a
# disk failure / theft doesn't take the backups with it. Override with the
# SNOWLINE_BACKUP_DIR env var in the plist to point at a tailnet rsync target,
# an external volume, etc.
DEST="${SNOWLINE_BACKUP_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/Snowline/db-backups}"
RETENTION_DAYS="${SNOWLINE_BACKUP_RETENTION_DAYS:-30}"

mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
FAILED=""

for DB in $DBS; do
    OUT="$DEST/${DB}-${STAMP}.dump"
    # Dump to a .partial name, then atomically rename on success — an
    # interrupted dump (machine sleep, OOM, iCloud hiccup) never leaves a
    # truncated file that looks like a complete backup. One store failing
    # must not skip the others: record it and keep going.
    if pg_dump -Fc "$DB" > "$OUT.partial" && mv "$OUT.partial" "$OUT"; then
        # Size is best-effort (an iCloud file can be evicted to a placeholder
        # the instant it lands, making du error).
        SIZE="$(du -h "$OUT" 2>/dev/null | cut -f1 || true)"
        echo "$(date -u +%FT%TZ) backed up ${DB} -> ${OUT} (${SIZE:-size unknown})"
    else
        rm -f "$OUT.partial"
        echo "$(date -u +%FT%TZ) FAILED to back up ${DB}" >&2
        FAILED="$FAILED $DB"
        continue
    fi

    # Prune this store's dumps past the retention window. Per-store pattern, so
    # dumps of stores no longer in the list (e.g. the legacy snowline_dev
    # history) are left alone. Non-fatal: a malformed RETENTION_DAYS or a find
    # hiccup must not make a run whose dump already succeeded read as failed.
    find "$DEST" -maxdepth 1 -name "${DB}-*.dump" -type f -mtime +"$RETENTION_DAYS" -delete || true
done

# Stale .partial leftovers from any store.
find "$DEST" -maxdepth 1 -name '*.partial' -type f -mtime +1 -delete || true

if [ -n "$FAILED" ]; then
    echo "$(date -u +%FT%TZ) run incomplete; failed:${FAILED}" >&2
    exit 1
fi
