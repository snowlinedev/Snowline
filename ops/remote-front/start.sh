#!/bin/sh
# Entrypoint for the fly deploy: bring up tailscaled (userspace networking) as a
# scoped tailnet node, expose its OUTBOUND HTTP proxy, and start the app pointed
# at that proxy. DEPLOY glue only — none of this is imported by the app.
#
# The app makes ordinary httpx calls to REMOTE_FRONT_UPSTREAM (a tailnet host);
# httpx honours HTTP(S)_PROXY (trust_env), so routing those calls THROUGH
# tailscaled's outbound proxy is how they traverse the tailnet — the proxy code
# never touches tailscale. tailscaled resolves the peer's MagicDNS name.
set -eu

TS_STATE_DIR="${TS_STATE_DIR:-/var/lib/tailscale}"
TS_HOSTNAME="${TS_HOSTNAME:-snowline-remote-front}"
TS_PROXY_PORT="${TS_PROXY_PORT:-1055}"

# TAILSCALE_AUTHKEY must be a fly secret for a TAGGED node (tag:remote-front) —
# see the ACL stanza in docs/ops/remote-front-runbook.md.
: "${TAILSCALE_AUTHKEY:?TAILSCALE_AUTHKEY is required (fly secret; tag:remote-front)}"

# fly.toml points TS_STATE_DIR at the mounted volume (/data/tailscale), which is
# EMPTY on first boot — the Dockerfile's mkdir is shadowed by the mount, so the
# state dir must be created here or tailscaled dies on its state file.
mkdir -p "${TS_STATE_DIR}" /var/run/tailscale

# The app runs as the unprivileged `app` user (see the setpriv exec below); the
# fly volume mounts root-owned, so hand the app's store path over. tailscale's
# own state stays root-owned (tailscaled runs as root, below).
if [ -n "${REMOTE_FRONT_STORE_PATH:-}" ]; then
    store_dir="$(dirname "${REMOTE_FRONT_STORE_PATH}")"
    mkdir -p "${store_dir}"
    chown app:app "${store_dir}"
    [ -e "${REMOTE_FRONT_STORE_PATH}" ] && chown app:app "${REMOTE_FRONT_STORE_PATH}"* || true
fi

/usr/sbin/tailscaled \
    --state="${TS_STATE_DIR}/tailscaled.state" \
    --socket=/var/run/tailscale/tailscaled.sock \
    --tun=userspace-networking \
    --outbound-http-proxy-listen="localhost:${TS_PROXY_PORT}" &

# Join the tailnet as the scoped node. --accept-dns=true so the primary's
# MagicDNS name in REMOTE_FRONT_UPSTREAM resolves through tailscaled.
tailscale up \
    --authkey="${TAILSCALE_AUTHKEY}" \
    --hostname="${TS_HOSTNAME}" \
    --accept-dns=true \
    --accept-routes=false

# Route the app's upstream HTTP calls over the tailnet via tailscaled's proxy.
export HTTP_PROXY="http://localhost:${TS_PROXY_PORT}"
export HTTPS_PROXY="http://localhost:${TS_PROXY_PORT}"
export http_proxy="http://localhost:${TS_PROXY_PORT}"
export https_proxy="http://localhost:${TS_PROXY_PORT}"
# The fly proxy talks to the app locally; never send loopback through the proxy.
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

# Drop privileges for the app process. tailscaled stays root (it owns the
# volume-backed node state and the entrypoint needed root for the first-boot
# volume chown anyway), but the PUBLIC-facing process — the one parsing
# attacker-supplied HTTP — runs as the unprivileged `app` user. It talks to
# tailscaled only via the localhost HTTP proxy, so it needs no socket access.
exec setpriv --reuid=app --regid=app --clear-groups snowline-remote-front
