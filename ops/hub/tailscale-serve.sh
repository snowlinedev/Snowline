#!/usr/bin/env bash
# Hosted interface for the HUB (the always-on desktop/mini, macOS distribution
# spec §1 — "the primary stays source-run"): a stable tailnet HTTPS URL for
# the composed gateway + dashboard, via `tailscale serve` (issue 39c092c9).
#
# ONE mapping covers everything. Unlike the roam spoke's port-preserving 1:1
# mirror (ops/roam/tailscale-serve.sh, one tailnet port per service), the hub
# needs only https:443 -> the platform's own port: the dashboard is served BY
# the platform itself, at /ui (+ /ui-api) on the SAME port as the gateway
# (src/snowline_platform/app.py — the composed surface, /ui, and /ui-api all
# live on one ASGI app). So gateway + dashboard + /ui-api ride a single
# front-end port, and `tailscale serve --https=443` fronting that one port is
# the whole job.
#
# TAILNET-ONLY. Funnel (public internet exposure) is an explicit NON-GOAL for
# this item (marker 183a6ad9) — `tailscale serve` alone never leaves the
# tailnet; nothing here calls `tailscale funnel`. See docs/ops/hub-serve.md.
#
# BACKEND ADDRESS: the hub's platform binds its TAILNET IP directly, not
# loopback (unlike the roaming spoke's loopback-first posture, ops/roam
# tailscale-serve.sh's front) — replication-continuity.md and
# docs/specs/deploy-continuity.md §4 both reference the hub's platform socket
# living on its tailnet address (e.g. `100.81.176.75:8850` as of that spec's
# writing). `tailscale serve`'s backend target must therefore point at the
# address the platform ACTUALLY listens on — never hardcoded, always this
# variable, defaulted by asking tailscaled for this node's own tailnet IPv4
# address at run time (so a re-issued tailnet IP never silently breaks this
# script). Override if the platform is ever reconfigured to bind loopback
# behind its own front:
#
#   SNOWLINE_HUB_BACKEND_HOST — defaults to `tailscale ip -4` (this node's
#                                own tailnet IPv4 address)
#   SNOWLINE_HUB_PLATFORM_PORT — defaults to 8850 (the hub's real gateway
#                                port — NOT the roam runbook's illustrative
#                                :8848, see docs/specs/macos-distribution.md's
#                                bootstrap-spoke section)
#
# Run ONCE on the hub (idempotent — re-running just re-applies the same
# config). `--bg` persists the serve config across `tailscaled`/reboots (it is
# written to tailscaled's own saved serve-config store, not this shell's
# state) — see docs/ops/hub-serve.md for the reboot-survival story in full.
#
# Usage:
#   ops/hub/tailscale-serve.sh          # configure (idempotent)
#   ops/hub/tailscale-serve.sh --status # print the current serve config only
#   ops/hub/tailscale-serve.sh --reset  # tear down ALL `tailscale serve` config
set -euo pipefail

BACKEND_HOST="${SNOWLINE_HUB_BACKEND_HOST:-$(tailscale ip -4)}"
BACKEND_PORT="${SNOWLINE_HUB_PLATFORM_PORT:-8850}"
BACKEND="http://${BACKEND_HOST}:${BACKEND_PORT}"

case "${1:-}" in
  --reset)
    echo "Resetting ALL tailscale serve config on this node..."
    tailscale serve reset
    exit 0
    ;;
  --status)
    tailscale serve status
    exit 0
    ;;
  "") ;;
  *)
    echo "usage: $0 [--status|--reset]" >&2
    exit 2
    ;;
esac

echo "Configuring tailscale serve (HTTPS, tailnet-only) -> ${BACKEND} ..."
# https:443 -> BACKEND covers the composed gateway, the dashboard at /ui, and
# the /ui-api data proxy in one mapping (they all live on the platform's one
# port). --bg backgrounds the config so it survives this shell exiting AND
# persists across tailscaled restarts/reboots (docs/ops/hub-serve.md).
tailscale serve --bg --https=443 "${BACKEND}"

echo
echo "Current serve config:"
tailscale serve status

STABLE_HOST="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("Self", {}).get("DNSName", "").rstrip("."))' 2>/dev/null || true)"
echo
if [ -n "$STABLE_HOST" ]; then
  echo "Done. Stable tailnet URL: https://${STABLE_HOST}/"
  echo "  (dashboard at https://${STABLE_HOST}/ui, gateway/API at the root)"
else
  echo "Done. Find the stable URL with: tailscale serve status"
fi
echo "Tailnet-only — Funnel is explicitly NOT configured (see this script's header)."
echo "Reset with: $0 --reset"
