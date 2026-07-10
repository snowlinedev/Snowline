"""The `OAuthAuthorizationServerProvider` implementation — the only OAuth code we
write. Everything protocol-shaped (metadata, PKCE verification, redirect_uri
matching, client auth, DCR request parsing) is the MCP SDK's; this class supplies
the storage + token minting + the single-user login seam.

Single resource owner: there is no user database. `authorize` parks the request
and redirects the browser to our `/login` page; on the correct fixed credential,
`complete_login` mints the authorization code and bounces back to the client's
redirect_uri. So the OAuth flow is standard authorization-code + PKCE, but the
"which user" question is answered by one shared credential, always the same
`subject`.

Abuse hardening (PR #122 security review): the pending-login set is TTL'd and
size-capped (an unauthenticated /authorize can't grow it without bound); a
pending txn is CONSUMED after `MAX_LOGIN_ATTEMPTS` failed passwords (no
unlimited guessing against one txn — see also the login throttle in login.py);
and open DCR is capped at `max_clients` stored registrations, evicting only
clients with no live refresh token (a registration flood can't fill the disk or
evict the live connector).
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from snowline_remote_front.store import Store
from snowline_remote_front.tokens import AccessTokenCodec

log = logging.getLogger("snowline_remote_front.provider")

# Authorization codes are one-time and consumed within seconds (the client
# exchanges immediately at /token). A short life bounds a leaked/replayed code.
AUTH_CODE_TTL = 120.0

# Pending-login (parked /authorize) hygiene. TTL is human-scale — the owner has
# 10 minutes to type the credential — and the size cap bounds memory against an
# /authorize hammer (each entry is small; 512 is far beyond any legitimate
# concurrent-login count for a single-owner front). Oldest-evicted at the cap.
PENDING_TTL = 600.0
MAX_PENDING = 512

# A pending txn is consumed after this many WRONG passwords: one /authorize
# grants at most 5 guesses, then the attacker must round-trip /authorize again
# (which the login throttle in login.py also slows down).
MAX_LOGIN_ATTEMPTS = 5


@dataclass
class _PendingAuth:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    created_at: float
    attempts: int = 0


class RemoteFrontProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(
        self,
        *,
        store: Store,
        codec: AccessTokenCodec,
        issuer_url: str,
        subject: str,
        access_ttl: int,
        refresh_ttl: int,
        canonical_resource: str,
        max_clients: int,
    ) -> None:
        self._store = store
        self._codec = codec
        self._issuer_url = issuer_url.rstrip("/")
        self._subject = subject
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl
        # Stamped as the `res` claim on EVERY minted access token, and checked
        # by the codec on the way back in (RFC 8707 audience binding): a token
        # minted for any other resource never authenticates here.
        self._canonical_resource = canonical_resource
        self._max_clients = max_clients
        # Pending authorizations awaiting the /login step, keyed by an opaque
        # transaction id. In-memory ONLY: an in-flight authorization is a live
        # session (issue #120 "lose nothing but live sessions on restart") — if
        # the app restarts mid-login the user just re-clicks connect. Nothing
        # here is a durable grant. Swept (TTL + size cap) on every insert.
        self._pending: dict[str, _PendingAuth] = {}

    # --- Dynamic client registration (RFC 7591) --------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Cap stored registrations (disk-fill DoS guard for open DCR): make room
        # by evicting the oldest client with NO live refresh token; if every
        # stored client has a live token (the cap is genuinely full of active
        # connectors), refuse the registration instead of breaking one.
        if not self._store.prune_clients(self._max_clients):
            log.warning(
                "remote-front: client registration refused — %d stored clients "
                "all hold live refresh tokens (cap: REMOTE_FRONT_MAX_CLIENTS)",
                self._max_clients,
            )
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="client registration limit reached",
            )
        self._store.put_client(client_info)

    # --- Authorization-code flow -----------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to our single-user login page.
        Returns the URL the SDK's authorize handler redirects to."""
        self._sweep_pending()
        txn = secrets.token_urlsafe(24)
        self._pending[txn] = _PendingAuth(
            client=client, params=params, created_at=time.time()
        )
        return f"{self._issuer_url}/login?txn={txn}"

    def _sweep_pending(self) -> None:
        """Drop expired pending logins; then, at the size cap, evict oldest
        (dict preserves insertion order, so the first key is the oldest)."""
        now = time.time()
        for txn in [
            t for t, p in self._pending.items() if now - p.created_at > PENDING_TTL
        ]:
            del self._pending[txn]
        while len(self._pending) >= MAX_PENDING:
            del self._pending[next(iter(self._pending))]

    def pending_login(self, txn: str) -> dict[str, str] | None:
        """The rendering context for a live pending authorization — client
        identity + redirect host, so the login page can show the owner WHO is
        asking and WHERE the browser will be sent (anti-phishing: a foreign
        DCR-registered client is visible before the credential is typed).
        Returns None for an unknown/expired txn."""
        entry = self._pending.get(txn)
        if entry is None:
            return None
        if time.time() - entry.created_at > PENDING_TTL:
            self._pending.pop(txn, None)
            return None
        client = entry.client
        return {
            "client_id": str(client.client_id),
            "client_name": client.client_name or str(client.client_id),
            "redirect_host": urlparse(str(entry.params.redirect_uri)).netloc,
        }

    def fail_login(self, txn: str) -> bool:
        """Record a failed password for `txn`. Returns True while the txn is
        still usable; at `MAX_LOGIN_ATTEMPTS` the txn is CONSUMED (returns
        False) — further guesses require a fresh /authorize round-trip."""
        entry = self._pending.get(txn)
        if entry is None:
            return False
        entry.attempts += 1
        if entry.attempts >= MAX_LOGIN_ATTEMPTS:
            del self._pending[txn]
            log.warning(
                "remote-front: login txn consumed after %d failed attempts",
                entry.attempts,
            )
            return False
        return True

    def complete_login(self, txn: str) -> str:
        """Called by the login POST after the fixed credential checks out: mint
        the authorization code and return the client redirect_uri carrying
        ``code`` (+ ``state``). Raises KeyError for an unknown/expired txn."""
        entry = self._pending.pop(txn)
        client, params = entry.client, entry.params
        code = secrets.token_urlsafe(32)
        self._store.put_auth_code(
            AuthorizationCode(
                code=code,
                scopes=params.scopes or [],
                expires_at=time.time() + AUTH_CODE_TTL,
                client_id=str(client.client_id),
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource,
                subject=self._subject,
            )
        )
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        auth_code = self._store.get_auth_code(authorization_code)
        if auth_code is None or auth_code.client_id != client.client_id:
            return None
        return auth_code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # One-time: burn the code so it can't be replayed (the SDK already
        # verified PKCE + redirect_uri + expiry before calling us).
        self._store.delete_auth_code(authorization_code.code)
        return self._issue(
            client_id=str(client.client_id),
            subject=authorization_code.subject or self._subject,
            scopes=authorization_code.scopes,
        )

    # --- Refresh ----------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = self._store.get_refresh_token(refresh_token)
        if stored is None or stored.client_id != client.client_id:
            return None
        return stored

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the presented refresh token is single-use. Delete it, then
        # issue a fresh access + refresh pair. A replay of the old refresh token
        # now loads nothing → invalid_grant.
        self._store.delete_refresh_token(refresh_token.token)
        return self._issue(
            client_id=str(client.client_id),
            subject=refresh_token.subject or self._subject,
            scopes=scopes or refresh_token.scopes,
        )

    # --- Token verification + revocation ---------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Stateless: verify the HMAC signature + expiry + resource (`res`,
        # RFC 8707) locally, no store hit.
        return self._codec.verify(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Only refresh tokens are revocable server-side (access tokens are
        # stateless + short-lived — they lapse on expiry). `token.token` is the
        # refresh string for a RefreshToken; for an AccessToken it's the signed
        # string, which is simply absent from the refresh store → a no-op.
        self._store.delete_refresh_token(token.token)

    # --- Internal ---------------------------------------------------------------

    def _issue(self, *, client_id: str, subject: str, scopes: list[str]) -> OAuthToken:
        # `res` is ALWAYS this front's canonical resource — never the
        # client-supplied value — so the codec's audience check (RFC 8707) has
        # exactly one accepted value and a token minted for any other resource
        # (or with no `res` at all) never authenticates against the proxy.
        access_token, _expires_at = self._codec.mint(
            client_id=client_id,
            subject=subject,
            scopes=scopes,
            ttl=self._access_ttl,
            resource=self._canonical_resource,
        )
        refresh_token = secrets.token_urlsafe(32)
        self._store.put_refresh_token(
            RefreshToken(
                token=refresh_token,
                client_id=client_id,
                scopes=scopes,
                expires_at=int(time.time()) + self._refresh_ttl,
                subject=subject,
            )
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=self._access_ttl,
            refresh_token=refresh_token,
            scope=" ".join(scopes) if scopes else None,
        )
