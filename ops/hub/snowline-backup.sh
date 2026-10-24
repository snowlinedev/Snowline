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

# Space-separated list of databases to dump. Every live store on the hub —
# the four services plus musher and marketing. The cross-check below refuses
# to let this list drift from the *_DATABASE_URL values the hub's units
# actually run against.
DBS="${SNOWLINE_BACKUP_DBS:-snowline_platform snowline_pm snowline_governance snowline_memory snowline_musher snowline_marketing}"
ICLOUD_ROOT="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
# Off-machine by default: iCloud Drive replicates the dumps off this host, so a
# disk failure / theft doesn't take the backups with it. Override with the
# SNOWLINE_BACKUP_DIR env var in the plist to point at a tailnet rsync target,
# an external volume, etc.
DEST="${SNOWLINE_BACKUP_DIR:-$ICLOUD_ROOT/Snowline/db-backups}"
RETENTION_DAYS="${SNOWLINE_BACKUP_RETENTION_DAYS:-30}"

now() { date -u +%FT%TZ; }
die() { echo "$(now) $*" >&2; exit 1; }

# --- destination: refuse to write "backups" that never leave the machine ---
# `mkdir -p` of the full default path would silently recreate the iCloud
# folder as a plain local directory when iCloud Drive is signed out or not yet
# mounted, and every run would then log success while nothing is replicated.
# Only the leaf may be created; the iCloud root must already exist.
case "$DEST" in
    "$ICLOUD_ROOT"/*)
        [ -d "$ICLOUD_ROOT" ] || die "iCloud Drive root missing ($ICLOUD_ROOT); refusing to write local-only backups — is iCloud Drive signed in?"
        ;;
esac
[ -d "$(dirname "$DEST")" ] || die "backup destination parent missing: $(dirname "$DEST")"
mkdir -p "$DEST" || die "cannot create backup destination: $DEST"
[ -w "$DEST" ] || die "backup destination not writable: $DEST"
chmod 700 "$DEST" 2>/dev/null || true   # best effort: an iCloud folder may refuse

# --- cross-check: every store a hub unit is configured against must be listed ---
# Sources: the service env files and the per-unit EnvironmentVariables of
# every dev.snowline.* launchd plist (musher / marketing carry their URL there).
# A store that is configured but not in DBS is reported and fails the run —
# the "exit 0 while the live store is unprotected" failure class.
configured_dbs() {
    {
        grep -h -o -E '^[A-Z_]+_DATABASE_URL=.*' "$HOME"/.config/snowline/*.env 2>/dev/null \
            | grep -v -E '^[A-Z_]*(TEST|DRYRUN)[A-Z_]*_DATABASE_URL' \
            | cut -d= -f2-
        for plist in "$HOME"/Library/LaunchAgents/dev.snowline.*.plist /Library/LaunchDaemons/dev.snowline.*.plist; do
            [ -f "$plist" ] || continue
            grep -A1 -E '[A-Z_]+_DATABASE_URL</key>' "$plist" 2>/dev/null \
                | grep -o -E 'postgres[a-z+]*://[^<]*'
        done
    } | sed -e 's#[?].*##' -e 's#.*/##' | grep -v '^$' | sort -u
}
UNLISTED=""
for db in $(configured_dbs); do
    case " $DBS " in
        *" $db "*) ;;
        *) UNLISTED="$UNLISTED $db" ;;
    esac
done
[ -z "$UNLISTED" ] || echo "$(now) WARNING: configured but not in SNOWLINE_BACKUP_DBS:${UNLISTED} — add them or set SNOWLINE_BACKUP_DBS explicitly" >&2

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
[ -z "$UNLISTED" ] || exit 1
