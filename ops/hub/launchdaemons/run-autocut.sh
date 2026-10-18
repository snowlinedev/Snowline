#!/bin/sh
# Wrapper for the hub's hourly auto-cut timer (dev.snowline.autocut, #258).
#
# 1. kill switch: exits 0 at once while ~/.config/snowline/autocut.disabled
#    exists (disable without sudo: `touch` it; re-enable: `rm` it)
# 2. sources ~/.config/snowline/autocut.env when present (optional; e.g. the
#    *_TEST_DATABASE_URL vars the cut's per-repo test runs pick up)
# 3. execs the platform checkout's OWN venv entry point — the source-run
#    posture the services use (run-service.sh execs .venv/bin/uvicorn). Not
#    `uv run`: that would re-sync the venv the running hub services import.
#
# Exit codes are autocut's: 0 noop/ok/locked, 2 gated (nothing cut), 1 failed.

home="${HOME:-/Users/seanlynch}"
repo=/Users/seanlynch/Projects/snowline-platform

if [ -e "$home/.config/snowline/autocut.disabled" ]; then
  echo "$(date '+%Y-%m-%dT%H:%M:%S') autocut: disabled ($home/.config/snowline/autocut.disabled)"
  exit 0
fi

if [ -f "$home/.config/snowline/autocut.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$home/.config/snowline/autocut.env"
  set +a
fi

cd "$repo" || exit 1
exec "$repo/.venv/bin/snowline" release autocut
