#!/bin/bash
# Hourly backup of every live Snowline Postgres store. Run by the per-user
# LaunchAgent ops/hub/launchagents/dev.snowline.backup.plist (installed by
# ops/hub/install-backup-agent.sh). One-shot: it dumps, prunes and exits; a
# failed run is a skipped hour, and the next fire takes a fresh dump.
#
# The stores (one database per service) are the only copy of scopes,
# milestones, work items, decisions, artifacts and memory: none of it is in
# git. Decision e4578136 governs: hourly, off-machine, secrets NOT backed up.
#
# Restore one store (destructive, to the live DB):
#   pg_restore --clean --if-exists --no-owner -d snowline_pm <dump>
# Restore to a scratch DB to inspect first:
#   createdb snowline_restore && pg_restore --no-owner -d snowline_restore <dump>
#
# Overrides (environment): SNOWLINE_BACKUP_DIR (destination),
# SNOWLINE_BACKUP_RETENTION_DAYS (default 30), SNOWLINE_BACKUP_DBS
# (space-separated store list, replacing the catalog lookup).
# Exit: 0 all stores dumped; 1 a store failed; 75 another run holds the lock.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

log() { echo "$(date -u +%FT%TZ) $*"; }

# One run at a time: a lock directory holding the owner's pid. A lock whose
# pid is gone (a run killed by SIGKILL or power loss) is taken over.
LOCK="$HOME/Library/Application Support/Snowline/backup.lock.d"
mkdir -p "$(dirname "$LOCK")"
if ! mkdir "$LOCK" 2>/dev/null; then
    holder="$(cat "$LOCK/pid" 2>/dev/null)"
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
        log "another backup run (pid $holder) holds the lock; stepping aside"; exit 75
    fi
    rm -rf "$LOCK"; mkdir "$LOCK" || { log "cannot take the lock $LOCK" >&2; exit 2; }
fi
echo $$ > "$LOCK/pid"
# Every run ends with one "run exit <code>" line; the installer reads it.
trap 'rc=$?; rm -rf "$LOCK"; log "run exit $rc"' EXIT
# bash runs a trap only when the foreground command returns, so each dump runs
# in the background and is waited for; a TERM (bootout, logout) then removes
# the partial at once.
OUT=""; CHILD=""
trap 'log "interrupted"; [ -z "$CHILD" ] || kill "$CHILD" 2>/dev/null; [ -z "$OUT" ] || rm -f "$OUT.partial"; exit 143' TERM INT HUP
log "run start"

DEST="${SNOWLINE_BACKUP_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/Snowline/db-backups}"
RETENTION_DAYS="${SNOWLINE_BACKUP_RETENTION_DAYS:-30}"
case "$RETENTION_DAYS" in ''|*[!0-9]*|0) log "bad SNOWLINE_BACKUP_RETENTION_DAYS: '$RETENTION_DAYS'" >&2; exit 2 ;; esac
mkdir -p "$DEST" || { log "cannot create $DEST" >&2; exit 2; }
umask 077   # dumps hold every store's data: owner-only

# At boot launchd can fire this before Postgres (the owner's agent) is up.
waited=0
until pg_isready -q; do
    [ "$waited" -ge 120 ] && { log "postgres not ready after 120s; trying anyway"; break; }
    sleep 5; waited=$((waited + 5))
done

# The store list: every snowline_* database the server has, minus scratch ones,
# so a new service's store is covered from its first hour. If the catalog
# cannot be read, the known stores are tried (and fail loudly if absent).
FALLBACK="snowline_governance snowline_marketing snowline_memory snowline_musher snowline_platform snowline_pm"
SCRATCH='(_(test|dryrun|e2e|restore)(_|$)|^snowline_dev$)'
if [ -n "${SNOWLINE_BACKUP_DBS:-}" ]; then
    DBS="$SNOWLINE_BACKUP_DBS"
elif CATALOG="$(psql -X -At -d postgres -c "select datname from pg_database where datname like 'snowline\_%' and datallowconn and not datistemplate order by 1")"; then
    DBS="$(printf '%s\n' "$CATALOG" | grep -Ev "$SCRATCH" | tr '\n' ' ')"
    [ -n "${DBS// /}" ] || { log "no snowline_* stores in the catalog (wrong cluster?); trying the known list" >&2; DBS="$FALLBACK"; }
else
    log "could not read the catalog; trying the known list" >&2
    DBS="$FALLBACK"
fi

STAMP="$(date -u +%Y%m%d-%H%M%S)Z"
CUTOFF="$(date -u -v-"${RETENTION_DAYS}"d +%Y%m%d-%H%M%S)"
KEEP=3   # a store's newest three dumps are kept whatever their age
FAILED=""
for DB in $DBS; do
    OUT="$DEST/${DB}-${STAMP}.dump"
    # Dump to .partial, check the archive reads back, then rename: an
    # interrupted or truncated dump never looks like a complete backup.
    # --lock-wait-timeout: a migration holding ACCESS EXCLUSIVE must not be
    # blocked by the dump; better a failed store this hour.
    pg_dump -Fc --lock-wait-timeout=60000 "$DB" > "$OUT.partial" &
    CHILD=$!; wait "$CHILD"; rc=$?; CHILD=""
    if [ "$rc" -eq 0 ] && pg_restore -l "$OUT.partial" > /dev/null && mv "$OUT.partial" "$OUT"; then
        OUT=""
        log "backed up ${DB} ($(stat -f %z "$DEST/${DB}-${STAMP}.dump" 2>/dev/null || echo '?') bytes)"
    else
        rm -f "$OUT.partial"; OUT=""
        log "FAILED to back up ${DB}" >&2
        FAILED="$FAILED $DB"
    fi

    # Prune this store's dumps (and their iCloud .NAME.icloud placeholders)
    # older than the retention window, by the stamp in the name, keeping the
    # newest three so a store that keeps failing never loses its last copy.
    ls -1A "$DEST" | sed -nE "s/^(\.?${DB}-([0-9]{8}-[0-9]{6})Z?\.dump(\.icloud)?)$/\2 \1/p" \
        | sort -r | tail -n +$((KEEP + 1)) \
        | while read -r stamp name; do [ "$stamp" \< "$CUTOFF" ] && rm -f "$DEST/$name"; done
done

# Staging files left by a run that died without its trap (SIGKILL, power loss).
find "$DEST" -maxdepth 1 -type f -name 'snowline_*.dump.partial' -mmin +120 -delete 2>/dev/null

if [ -n "$FAILED" ]; then
    log "run incomplete; failed:${FAILED}" >&2
    exit 1
fi
log "run complete"
