#!/usr/bin/env bash
# Launch one Snowline service bound to LOOPBACK ONLY (replication-continuity
# §5.1), with the correct per-process replication source id. Used by the launchd
# plists in ops/roam/launchd/ and runnable by hand for the drill.
#
#   ./run-service.sh <platform|governance|memory|pm>
#
# Env: SNOWLINE_ENV_FILE points at your filled-in env.roam / env.primary
# (defaults to ops/roam/env.roam.example — override it). SNOWLINE_REPO points at
# the platform checkout (defaults to two dirs up from this script).
# SNOWLINE_PM_REPO points at the pm checkout (defaults to ../snowline-pm next
# to the platform checkout — the same sibling layout release/components.json
# uses) — pm is a SEPARATE repo, not a platform workspace member (macOS
# distribution spec §7/§8: pm joins the spoke drill, item b70b0359).
set -euo pipefail

service="${1:?usage: run-service.sh <platform|governance|memory|pm>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="${SNOWLINE_REPO:-$(cd "$here/../.." && pwd)}"
pm_repo="${SNOWLINE_PM_REPO:-$repo/../snowline-pm}"
env_file="${SNOWLINE_ENV_FILE:-$here/env.roam.example}"

# shellcheck source=/dev/null
set -a; source "$env_file"; set +a

case "$service" in
  platform)   module="snowline_platform.app:app";   port="${SNOWLINE_PLATFORM_PORT:-8848}";   run_dir="$repo" ;;
  governance) module="snowline_governance.app:app"; port="${SNOWLINE_GOVERNANCE_PORT:-8801}"; run_dir="$repo" ;;
  memory)     module="snowline_memory.app:app";     port="${SNOWLINE_MEMORY_PORT:-8802}";     run_dir="$repo" ;;
  pm)         module="snowline_pm.app:app";         port="${SNOWLINE_PM_PORT:-8803}";         run_dir="$pm_repo" ;;
  *) echo "unknown service: $service" >&2; exit 2 ;;
esac

# The per-PROCESS replication source id is <instance>.<service> (§3) — set here
# so each process gets its own (a single global would fork stream identity).
export SNOWLINE_REPLICATION_SOURCE_ID="${SNOWLINE_INSTANCE_ID}.${service}"

echo "starting ${service} as ${SNOWLINE_REPLICATION_SOURCE_ID} on 127.0.0.1:${port}"
cd "$run_dir"
# --host 127.0.0.1 is LOAD-BEARING: never 0.0.0.0 on the roaming laptop (§5.1).
exec uv run uvicorn "$module" --host 127.0.0.1 --port "$port"
