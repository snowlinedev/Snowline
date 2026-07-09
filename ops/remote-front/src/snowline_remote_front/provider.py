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
"""

from __future__ import annotations

import logging
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from snowline_remote_front.store import Store
from snowline_remote_front.tokens import AccessTokenCodec

log = logging.getLogger("snowline_remote_front.provider")

# Authorization codes are one-time and consumed within seconds (the client
# exchanges immediately at /token). A short life bounds a leaked/replayed code.
AUTH_CODE_TTL = 120.0


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
    ) -> None:
        self._store = store
        self._codec = codec
        self._issuer_url = issuer_url.rstrip("/")
        self._subject = subject
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl
        # Pending authorizations awaiting the /login step, keyed by an opaque
        # transaction id. In-memory ONLY: an in-flight authorization is a live
        # session (issue #120 "lose nothing but live sessions on restart") — if
        # the app restarts mid-login the user just re-clicks connect. Nothing
        # here is a durable grant.
        self._pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams]] = {}

    # --- Dynamic client registration (RFC 7591) --------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._store.put_client(client_info)

    # --- Authorization-code flow -----------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to our single-user login page.
        Returns the URL the SDK's authorize handler redirects to."""
        txn = secrets.token_urlsafe(24)
        self._pending[txn] = (client, params)
        return f"{self._issuer_url}/login?txn={txn}"

    def pending_login(self, txn: str) -> bool:
        """Whether `txn` names a live pending authorization (for the login GET)."""
        return txn in self._pending

    def complete_login(self, txn: str) -> str:
        """Called by the login POST after the fixed credential checks out: mint
        the authorization code and return the client redirect_uri carrying
        ``code`` (+ ``state``). Raises KeyError for an unknown/expired txn."""
        client, params = self._pending.pop(txn)
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
            resource=authorization_code.resource,
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
            resource=None,
        )

    # --- Token verification + revocation ---------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Stateless: verify the HMAC signature + expiry locally, no store hit.
        return self._codec.verify(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Only refresh tokens are revocable server-side (access tokens are
        # stateless + short-lived — they lapse on expiry). `token.token` is the
        # refresh string for a RefreshToken; for an AccessToken it's the signed
        # string, which is simply absent from the refresh store → a no-op.
        self._store.delete_refresh_token(token.token)

    # --- Internal ---------------------------------------------------------------

    def _issue(
        self, *, client_id: str, subject: str, scopes: list[str], resource: str | None
    ) -> OAuthToken:
        access_token, _expires_at = self._codec.mint(
            client_id=client_id,
            subject=subject,
            scopes=scopes,
            ttl=self._access_ttl,
            resource=resource,
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
