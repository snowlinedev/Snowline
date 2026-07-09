"""Snowline remote front (issue #120, Stage 1).

A thin, stateless public MCP endpoint for Claude.ai custom connectors. It
terminates public TLS (fly-side) + MCP OAuth, and reverse-proxies Streamable-HTTP
MCP to the primary gateway's chosen surface over the tailnet — ONLY for requests
bearing a valid access token. Zero Snowline platform code changes; no Snowline
domain data in the cloud (architecture §3.5 OAuth seam, exercised outside the
platform).

The app is a plain ASGI app (`create_app`), fully testable against a mock
upstream. fly.io + tailscale are DEPLOY artifacts only (see `fly.toml`,
`Dockerfile`, and `docs/ops/remote-front-runbook.md`) — never imported here.
"""

from snowline_remote_front.app import create_app
from snowline_remote_front.config import Config

__all__ = ["create_app", "Config"]
