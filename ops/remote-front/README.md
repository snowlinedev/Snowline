# snowline-remote-front

Issue #120, Stage 1 — a thin, stateless public MCP endpoint for **Claude.ai
custom connectors**. It terminates public TLS (fly-side) + MCP OAuth 2.1, and
reverse-proxies Streamable-HTTP MCP to the primary gateway's chosen surface over
the tailnet, **only** for requests bearing a valid access token.

- **OAuth** is the official MCP Python SDK's auth framework (`mcp.server.auth`):
  RFC 8414 AS metadata, RFC 9728 protected-resource metadata, RFC 7591 dynamic
  client registration, authorization-code + PKCE (S256), refresh tokens. We
  implement only the `OAuthAuthorizationServerProvider` storage/token seam plus
  a single-user `/login` gate — no hand-rolled OAuth.
- **Proxy**: forwards POST + SSE, passing MCP session headers both ways; a bare/
  bad/expired token gets a spec-correct `401 WWW-Authenticate` (which triggers
  Claude.ai's discovery); nothing unauthenticated ever reaches the tailnet.
- **Stateless**: client registrations + refresh tokens live in a tiny SQLite
  store (a fly volume); no Snowline domain data ever. Restarting loses nothing
  but live sessions.

The app is a plain ASGI app (`snowline_remote_front.create_app`), fully testable
against a mock upstream. **fly.io + tailscale are DEPLOY artifacts only**
(`Dockerfile`, `start.sh`, `fly.toml`) — never imported by the app.

## Run locally

```sh
uv run --package snowline-remote-front python -m pytest ops/remote-front/tests
# or serve it (env from env.example):
REMOTE_FRONT_ISSUER_URL=http://localhost:8080 \
REMOTE_FRONT_UPSTREAM=http://127.0.0.1:8850/remote/mcp \
REMOTE_FRONT_OWNER_PASSWORD=dev \
uv run snowline-remote-front
```

## Deploy

See [`docs/ops/remote-front-runbook.md`](../../docs/ops/remote-front-runbook.md):
fly launch/deploy, the three fly secrets, the Tailscale ACL stanza scoping
`tag:remote-front` to only the gateway port, the `SNOWLINE_SURFACES` composition,
and the Claude.ai connector-add walkthrough.
