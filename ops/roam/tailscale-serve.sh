#!/usr/bin/env bash
# Expose the loopback-bound Snowline PLATFORM on the tailnet via tailscaled
# (replication-continuity §5.1). The apps NEVER bind the tailnet address
# themselves — losing tailscaled must not take down the local agent's loopback
# access (that is half the spoke's job), and a wildcard bind would park a
# pre-auth listener on every untrusted LAN the laptop joins.
#
# ONLY THE PLATFORM PORT is mirrored (tailnet:8848 -> 127.0.0.1:8848). Plugin
# ports (8801/8802/8803) are deliberately NOT exposed: a peer instance reaches a
# plugin's replication surfaces THROUGH THIS GATEWAY at `/via/<plugin>/…`, which
# proxies to the plugin's loopback bind (§4.1, governance decision 0b8390f7).
# The pairing CLI and the seed address peers that way automatically.
#
# Run once per SPOKE instance. (The hub's platform binds its tailnet address
# directly and fronts it with ops/hub/tailscale-serve.sh instead.) `tailscale
# serve` config persists across reboots. Requires tailscaled up and this node
# logged in.
#
# NOTE: `tailscale serve` terminates the tailnet connection and forwards to
# loopback, so EVERY forwarded request reaches the platform with a 127.0.0.1
# peer IP — which is exactly why SNOWLINE_TRUSTED_CIDRS must include the
# loopback entries (§5.1). If you switch to a source-IP-preserving front
# instead, the tailnet range in the CIDR list is what carries the trust; keep
# both listed.
#
# A mirror left over from the pre-0b8390f7 posture (tailnet:8801/8802/8803) is
# harmless but pointless; remove it with `tailscale serve --tcp=<port> off`.
set -euo pipefail
PLATFORM_PORT="${SNOWLINE_PLATFORM_PORT:-8848}"

# The macOS GUI app does not put `tailscale` on PATH; its CLI lives inside the
# app bundle and MUST be invoked by its real path (a symlink trips the app's
# bundle-identifier check and aborts). Honor an explicit override first.
if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE="${TAILSCALE_BIN:-tailscale}"
else
  TAILSCALE="${TAILSCALE_BIN:-/Applications/Tailscale.app/Contents/MacOS/Tailscale}"
fi
tailscale() { "$TAILSCALE" "$@"; }

echo "Configuring tailscale serve (TCP, platform port only) -> loopback..."
echo "  tailnet:${PLATFORM_PORT} -> 127.0.0.1:${PLATFORM_PORT}"
tailscale serve --bg --tcp "${PLATFORM_PORT}" "tcp://127.0.0.1:${PLATFORM_PORT}"
echo
echo "Current serve config:"
tailscale serve status
echo
echo "Done. This host's gateway is reachable on the tailnet at"
echo "  http://$(tailscale ip -4):${PLATFORM_PORT}"
echo "Plugins are reached by peers through it at /via/<plugin>/… — no plugin port is exposed."
echo "Reset with: tailscale serve reset"
