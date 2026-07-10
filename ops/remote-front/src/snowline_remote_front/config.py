"""Remote-front configuration — env-driven, no secrets in code.

Every knob is an env var; `Config.from_env()` reads them once at boot. The app
logic takes a `Config` object, so tests build one explicitly and never touch the
process environment. fly.io injects the secrets (signing key, owner credential)
and the upstream/issuer URLs as ordinary env + `fly secrets` — see the runbook.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

log = logging.getLogger("snowline_remote_front.config")


class ConfigError(ValueError):
    """A missing/malformed remote-front config value, raised at boot. We FAIL
    LOUD on the load-bearing values (issuer, upstream, owner credential) rather
    than defaulting them, because a silent default here is either an open door
    (empty owner password) or a dead endpoint (no upstream)."""


# Access tokens are deliberately SHORT-LIVED (issue #120: "short-lived bearer
# ... refresh supported so the connector doesn't need weekly re-auth"). 15
# minutes bounds the blast radius of a leaked access token; the refresh token
# (30 days) is what keeps an idle connector working without a manual re-auth.
DEFAULT_ACCESS_TTL = 900
DEFAULT_REFRESH_TTL = 60 * 60 * 24 * 30

# The single fixed resource owner's subject id (RFC 7662/9068 `sub`). Single-user
# by design — there is no user database; the "login" step is one fixed credential.
DEFAULT_SUBJECT = "owner"

# Bound the CONNECT to the upstream so a down primary / severed tailnet fails
# fast with a clean 502 instead of hanging (issue #120 acceptance: "the front
# returns clean upstream errors, not hangs"). The READ timeout is intentionally
# unbounded (see proxy.py) so a long-lived SSE stream is not cut off.
DEFAULT_UPSTREAM_CONNECT_TIMEOUT = 10.0

# Cap on STORED client registrations (open DCR + a public endpoint = a
# disk-fill vector on the fly volume otherwise). At the cap the store evicts
# the oldest client with no live refresh token; if every stored client is
# active, new registrations are refused. 256 is orders of magnitude beyond a
# single owner's real connector count while keeping worst-case disk use tiny.
DEFAULT_MAX_CLIENTS = 256


@dataclass(frozen=True)
class Config:
    # The public OAuth issuer / authorization-server base URL, fly-terminated
    # TLS (e.g. https://snowline-remote.fly.dev). AS metadata + /authorize +
    # /token + /register hang off this.
    issuer_url: str
    # The MCP resource URL Claude.ai is pointed at as a connector (RFC 9728
    # resource identifier), e.g. https://snowline-remote.fly.dev/mcp. Defaults to
    # issuer_url + "/mcp".
    resource_url: str
    # The upstream gateway surface over the tailnet, e.g.
    # http://mini.tailXXXX.ts.net:8850/remote/mcp. Config, NOT hardwired `main`
    # (issue #120 §4): point it at a dedicated read-heavy surface.
    upstream_url: str
    # The single fixed resource-owner credential checked at the /login step.
    owner_password: str
    # HMAC key that signs (and locally verifies) access tokens. A fly secret in
    # deploy; an ephemeral per-process key is generated with a WARNING if unset
    # (fine for local/dev — refresh tokens live in the store, so even a rotated
    # signing key doesn't force a re-auth: a stale access token 401s, the client
    # refreshes, and gets a token under the new key).
    signing_key: str
    subject: str = DEFAULT_SUBJECT
    access_ttl: int = DEFAULT_ACCESS_TTL
    refresh_ttl: int = DEFAULT_REFRESH_TTL
    upstream_connect_timeout: float = DEFAULT_UPSTREAM_CONNECT_TIMEOUT
    # Path to a SQLite file for the persistent store (client registrations +
    # refresh tokens). Unset → in-memory store (tests, and the accepted "lose
    # live sessions on restart" degradation if no volume is attached). With a fly
    # volume the file persists, so restarting the app never forces re-adding the
    # connector (issue #120 acceptance).
    store_path: str | None = None
    # Cap on stored DCR client registrations (see DEFAULT_MAX_CLIENTS).
    max_clients: int = DEFAULT_MAX_CLIENTS

    @property
    def resource_path(self) -> str:
        """The path component of `resource_url` — where the protected MCP proxy
        is mounted (e.g. "/mcp")."""
        return urlparse(self.resource_url).path or "/mcp"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)

        def required(key: str) -> str:
            val = env.get(key, "").strip()
            if not val:
                raise ConfigError(
                    f"{key} is required (see docs/ops/remote-front-runbook.md)"
                )
            return val

        issuer = required("REMOTE_FRONT_ISSUER_URL").rstrip("/")
        resource = env.get("REMOTE_FRONT_RESOURCE_URL", "").strip() or f"{issuer}/mcp"
        upstream = required("REMOTE_FRONT_UPSTREAM")
        owner_password = required("REMOTE_FRONT_OWNER_PASSWORD")

        signing_key = env.get("REMOTE_FRONT_SIGNING_KEY", "").strip()
        if not signing_key:
            signing_key = secrets.token_urlsafe(48)
            log.warning(
                "REMOTE_FRONT_SIGNING_KEY unset — generated an ephemeral signing "
                "key. Access tokens issued now become invalid on the next "
                "restart (clients recover via refresh token). Set a fly secret "
                "for stable access tokens."
            )

        return cls(
            issuer_url=issuer,
            resource_url=resource,
            upstream_url=upstream,
            owner_password=owner_password,
            signing_key=signing_key,
            subject=env.get("REMOTE_FRONT_SUBJECT", "").strip() or DEFAULT_SUBJECT,
            access_ttl=int(env.get("REMOTE_FRONT_ACCESS_TTL") or DEFAULT_ACCESS_TTL),
            refresh_ttl=int(env.get("REMOTE_FRONT_REFRESH_TTL") or DEFAULT_REFRESH_TTL),
            upstream_connect_timeout=float(
                env.get("REMOTE_FRONT_UPSTREAM_CONNECT_TIMEOUT")
                or DEFAULT_UPSTREAM_CONNECT_TIMEOUT
            ),
            store_path=env.get("REMOTE_FRONT_STORE_PATH", "").strip() or None,
            max_clients=int(
                env.get("REMOTE_FRONT_MAX_CLIENTS") or DEFAULT_MAX_CLIENTS
            ),
        )
