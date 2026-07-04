#!/usr/bin/env bash
# Expose the loopback-bound Snowline services on the tailnet via tailscaled
# (replication-continuity §5.1). The apps NEVER bind the tailnet address
# themselves — losing tailscaled must not take down the local agent's loopback
# access (that is half the spoke's job), and a wildcard bind would park a
# pre-auth listener on every untrusted LAN the laptop joins.
#
# The mapping is PORT-PRESERVING and 1:1 (tailnet:PORT -> 127.0.0.1:PORT) for
# every service. That is the posture the pairing CLI assumes when it rewrites a
# peer plugin's loopback base_url onto the peer's tailnet host (§4.1,
# replication_pairing._rehost): the peer's governance at loopback :8801 is
# reachable at <this-host>.tailnet:8801, memory :8802 at :8802, and the platform
# :8848 at :8848.
#
# Run once per instance (primary AND roam). `tailscale serve` config persists
# across reboots. Requires tailscaled up and this node logged in.
#
# NOTE: `tailscale serve` terminates the tailnet connection and forwards to
# loopback, so EVERY forwarded request reaches the app with a 127.0.0.1 peer IP
# — which is exactly why SNOWLINE_TRUSTED_CIDRS must include the loopback
# entries (§5.1). If you switch to a source-IP-preserving front instead, the
# tailnet range in the CIDR list is what carries the trust; keep both listed.
set -euo pipefail

PLATFORM_PORT="${SNOWLINE_PLATFORM_PORT:-8848}"
GOV_PORT="${SNOWLINE_GOVERNANCE_PORT:-8801}"
MEM_PORT="${SNOWLINE_MEMORY_PORT:-8802}"

echo "Configuring tailscale serve (TCP, port-preserving) -> loopback..."
for port in "$PLATFORM_PORT" "$GOV_PORT" "$MEM_PORT"; do
  echo "  tailnet:${port} -> 127.0.0.1:${port}"
  tailscale serve --bg --tcp "${port}" "tcp://127.0.0.1:${port}"
done

echo
echo "Current serve config:"
tailscale serve status
echo
echo "Done. This host's services are now reachable on the tailnet at"
echo "  http://\$(tailscale ip -4):{${PLATFORM_PORT},${GOV_PORT},${MEM_PORT}}"
echo "Reset with: tailscale serve reset"
