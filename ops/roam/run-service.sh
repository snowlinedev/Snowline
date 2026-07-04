#!/usr/bin/env bash
# Launch one Snowline service bound to LOOPBACK ONLY (replication-continuity
# §5.1), with the correct per-process replication source id. Used by the launchd
# plists in ops/roam/launchd/ and runnable by hand for the drill.
#
#   ./run-service.sh <platform|governance|memory>
#
# Env: SNOWLINE_ENV_FILE points at your filled-in env.roam / env.primary
# (defaults to ops/roam/env.roam.example — override it). SNOWLINE_REPO points at
# the checkout (defaults to two dirs up from this script).
set -euo pipefail

service="${1:?usage: run-service.sh <platform|governance|memory>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="${SNOWLINE_REPO:-$(cd "$here/../.." && pwd)}"
env_file="${SNOWLINE_ENV_FILE:-$here/env.roam.example}"

# shellcheck source=/dev/null
set -a; source "$env_file"; set +a

case "$service" in
  platform)   module="snowline_platform.app:app";   port="${SNOWLINE_PLATFORM_PORT:-8848}" ;;
  governance) module="snowline_governance.app:app"; port="${SNOWLINE_GOVERNANCE_PORT:-8801}" ;;
  memory)     module="snowline_memory.app:app";     port="${SNOWLINE_MEMORY_PORT:-8802}" ;;
  *) echo "unknown service: $service" >&2; exit 2 ;;
esac

# The per-PROCESS replication source id is <instance>.<service> (§3) — set here
# so each process gets its own (a single global would fork stream identity).
export SNOWLINE_REPLICATION_SOURCE_ID="${SNOWLINE_INSTANCE_ID}.${service}"

echo "starting ${service} as ${SNOWLINE_REPLICATION_SOURCE_ID} on 127.0.0.1:${port}"
cd "$repo"
# --host 127.0.0.1 is LOAD-BEARING: never 0.0.0.0 on the roaming laptop (§5.1).
exec uv run uvicorn "$module" --host 127.0.0.1 --port "$port"
