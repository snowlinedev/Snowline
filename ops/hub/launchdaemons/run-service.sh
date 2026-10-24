#!/bin/sh
# Boot-time wrapper for the hub's system LaunchDaemons (issue 239ec238 / #239).
# Usage: run-service.sh <platform|pm|governance|memory>
#
# 1. sources ~/.config/snowline/<svc>.env (the daemon runs as UserName=seanlynch)
# 2. waits (bounded ~120s) for Postgres to accept connections — Homebrew's
#    postgres also starts at boot and may lose the race
# 3. platform only: waits (bounded ~180s) for the tailnet bind address to exist
#    on an interface (Tailscale may not be up yet; uvicorn binds before any app
#    code runs, so this cannot be retried in Python)
# 4. execs uvicorn.
# Every wait is logged to stdout (the service log). After a bounded wait we
# exec anyway: launchd's KeepAlive + ThrottleInterval then retry.

svc="$1"
home="${HOME:-/Users/seanlynch}"
repo_platform=/Users/seanlynch/Projects/snowline-platform
repo_pm=/Users/seanlynch/Projects/snowline-pm

case "$svc" in
  platform)   repo="$repo_platform"; pkg=snowline_platform;   host=100.81.176.75; port=8850; dburl_var=SNOWLINE_PLATFORM_DATABASE_URL ;;
  pm)         repo="$repo_pm";       pkg=snowline_pm;         host=127.0.0.1;     port=8802; dburl_var=SNOWLINE_PM_DATABASE_URL ;;
  governance) repo="$repo_platform"; pkg=snowline_governance; host=127.0.0.1;     port=8801; dburl_var=SNOWLINE_GOVERNANCE_DATABASE_URL ;;
  memory)     repo="$repo_platform"; pkg=snowline_memory;     host=127.0.0.1;     port=8803; dburl_var=SNOWLINE_MEMORY_DATABASE_URL ;;
  *) echo "usage: $0 platform|pm|governance|memory" >&2; exit 64 ;;
esac

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') run-service[$svc]: $*"; }

set -a
# shellcheck disable=SC1090
. "$home/.config/snowline/$svc.env"
set +a

# --- wait for Postgres (host parsed from the service's DATABASE_URL) ---
eval "dburl=\${$dburl_var:-}"
if [ -n "$dburl" ]; then
  # strip scheme and userinfo, then take the host up to '/', '?' or ':'
  rest="${dburl#*://}"
  rest="${rest##*@}"
  pghost="$(printf '%s' "$rest" | sed -e 's#[/?].*##' -e 's#:.*##')"
  # bounded wait shared with the backup timer (lib.sh); after it, start anyway
  # and let launchd's KeepAlive retry
  # shellcheck disable=SC1091
  . "$(dirname "$0")/lib.sh"
  wait_for_postgres "$pghost" || true   # it logs the timeout itself, via log()
fi

# --- platform: wait for the tailnet address to exist locally ---
if [ "$svc" = platform ]; then
  waited=0
  until ifconfig | grep -q "inet $host "; do
    if [ "$waited" -ge 180 ]; then
      log "$host never appeared after ${waited}s; starting anyway (launchd will retry)"
      break
    fi
    log "waiting for bind address $host (${waited}s)"
    sleep 5
    waited=$((waited + 5))
  done
fi

log "exec uvicorn $pkg.app:app on $host:$port"
cd "$repo" || exit 1
exec "$repo/.venv/bin/uvicorn" "$pkg.app:app" --host "$host" --port "$port"
