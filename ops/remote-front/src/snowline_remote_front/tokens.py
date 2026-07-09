"""Access tokens as locally-verifiable HMAC-signed strings.

Access tokens are STATELESS by design: the front signs a compact
``<base64url(payload)>.<base64url(hmac-sha256)>`` string and verifies it with the
same key on the way back in — no store lookup on the hot path, and a token
survives an app restart as long as the signing key does (a fly secret). This is
the "opaque tokens validated locally" option the issue offers; we don't emit
standard JWTs because the only consumer is this front (Claude.ai treats the
access token as opaque and just echoes it back as a bearer), so the extra JWT
surface/dependency buys nothing.

Refresh tokens are NOT signed self-describing tokens — they're random opaque
strings kept in the persistent store (see store.py), so they can be rotated and
revoked, and survive a signing-key rotation. That split is deliberate: a rotated
signing key invalidates outstanding access tokens (a stale one 401s), and the
client silently recovers by presenting its still-valid refresh token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from mcp.server.auth.provider import AccessToken


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class AccessTokenCodec:
    """Mints and verifies HMAC-signed access tokens with one signing key."""

    def __init__(self, signing_key: str) -> None:
        # Domain-separate from anything else that might reuse the same secret.
        self._key = hashlib.sha256(b"snowline-remote-front:at:" + signing_key.encode()).digest()

    def _sign(self, payload_b64: str) -> str:
        mac = hmac.new(self._key, payload_b64.encode("ascii"), hashlib.sha256).digest()
        return _b64u_encode(mac)

    def mint(
        self,
        *,
        client_id: str,
        subject: str,
        scopes: list[str],
        ttl: int,
        resource: str | None = None,
    ) -> tuple[str, int]:
        """Return ``(token, expires_at_epoch)``."""
        now = int(time.time())
        expires_at = now + ttl
        payload = {
            "cid": client_id,
            "sub": subject,
            "scp": scopes,
            "iat": now,
            "exp": expires_at,
            "jti": secrets.token_urlsafe(9),
            "res": resource,
        }
        payload_b64 = _b64u_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        return f"{payload_b64}.{self._sign(payload_b64)}", expires_at

    def verify(self, token: str) -> AccessToken | None:
        """Return the `AccessToken` if the signature is valid AND unexpired, else
        None. A tampered, malformed, or expired token verifies as None — the
        bearer backend then treats it as unauthenticated (a spec 401)."""
        try:
            payload_b64, signature = token.split(".", 1)
        except ValueError:
            return None
        if not hmac.compare_digest(signature, self._sign(payload_b64)):
            return None
        try:
            payload = json.loads(_b64u_decode(payload_b64))
        except (ValueError, json.JSONDecodeError):
            return None
        expires_at = payload.get("exp")
        if not isinstance(expires_at, int) or expires_at < int(time.time()):
            return None
        return AccessToken(
            token=token,
            client_id=payload["cid"],
            scopes=payload.get("scp") or [],
            expires_at=expires_at,
            resource=payload.get("res"),
            subject=payload.get("sub"),
        )
