# Snowline remote front (issue #120, Stage 1) — fly.io image with a tailscaled
# sidecar. DEPLOY artifact only: the app (src/snowline_remote_front) imports
# neither fly nor tailscale. tailscaled provides the OUTBOUND tailnet path to the
# primary gateway; the app reaches it via an ordinary HTTP proxy env var
# (HTTP(S)_PROXY), so the proxy code stays a plain httpx client with no tailscale
# awareness. Public TLS is fly's; this container serves plain HTTP on $PORT.
FROM python:3.12-slim

# uv for a fast, reproducible install of the workspace member.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# tailscaled + tailscale CLI (userspace networking — no NET_ADMIN/tun needed).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl iptables \
    && curl -fsSL https://tailscale.com/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install just this package. The build context is the ops/remote-front directory
# (see fly.toml's build.dockerfile + the note in the runbook), so the member is
# self-contained here.
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .

# tailscale runtime dirs + a state dir (a fly volume can be mounted at
# /var/lib/tailscale to persist the node identity across restarts).
RUN mkdir -p /var/run/tailscale /var/lib/tailscale /data

COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

ENV REMOTE_FRONT_HOST=0.0.0.0 \
    REMOTE_FRONT_PORT=8080
EXPOSE 8080

ENTRYPOINT ["/app/start.sh"]
