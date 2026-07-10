"""The single-user login step.

Claude.ai opens the /authorize URL in the resource owner's browser; the SDK
authorize handler (via `RemoteFrontProvider.authorize`) redirects here. This page
is the ONLY human touch point: one password field checked against the fixed
resource-owner credential. On success we mint the authorization code and 302 back
to the client's redirect_uri; there is no user database and no session cookie —
the transaction id in the URL carries the parked authorization.

Hardening (PR #122 security review):

  - The page renders WHO is asking (client name + client_id) and WHERE the
    browser will be sent (the redirect_uri host), so a phishing attempt via an
    attacker-registered DCR client is visible before the credential is typed.
  - A txn is CONSUMED after `provider.MAX_LOGIN_ATTEMPTS` wrong passwords
    (provider.fail_login), and every failure pays an exponential per-app delay
    (`_FailureThrottle`) — dependency-free brute-force damping on top of the
    high-entropy credential itself.
  - The credential comparison is over UTF-8 BYTES: `hmac.compare_digest`
    raises TypeError on non-ASCII str inputs, which would have turned a
    non-ASCII password guess into a 500.
"""

from __future__ import annotations

import hmac
import html

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from snowline_remote_front.provider import RemoteFrontProvider

# Exponential failure delay: the Nth consecutive wrong password (app-wide)
# waits base * 2^(N-1), capped. Module-level so tests can monkeypatch the base
# to ~0 (mirrors the platform's CONNECT_RETRY_BACKOFFS convention). App-wide
# (not per-IP) is the right shape for a single-owner front: the legitimate
# owner logs in a handful of times a month, so ANY sustained failure stream is
# an attack, and a global damper can't be dodged by rotating source IPs.
LOGIN_FAILURE_BASE_DELAY = 0.5
LOGIN_FAILURE_MAX_DELAY = 8.0

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Snowline remote access</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 24rem; margin: 4rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.1rem; }}
  input, button {{ font-size: 1rem; padding: .5rem; width: 100%; box-sizing: border-box; }}
  button {{ margin-top: .75rem; cursor: pointer; }}
  .err {{ color: #b00020; }}
  .who {{ background: #f4f4f4; border-radius: 6px; padding: .6rem .8rem; font-size: .9rem; }}
  .who code {{ word-break: break-all; }}
</style></head>
<body>
  <h1>Snowline remote access</h1>
  <p>Authorize this connector to reach your Snowline surface.</p>
  {who}
  {error}
  <form method="post" action="/login">
    <input type="hidden" name="txn" value="{txn}">
    <input type="password" name="password" placeholder="Resource-owner credential" autofocus autocomplete="current-password">
    <button type="submit">Authorize</button>
  </form>
</body></html>"""

_WHO = """<div class="who">
  <div><strong>Requesting client:</strong> {client_name}</div>
  <div><strong>Client id:</strong> <code>{client_id}</code></div>
  <div><strong>After login your browser is sent to:</strong> <code>{redirect_host}</code></div>
  <div>If this client or redirect host looks foreign, close this page —
  do NOT enter the credential.</div>
</div>"""


def _render(
    txn: str,
    *,
    context: dict[str, str] | None = None,
    error: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    error_html = f'<p class="err">{html.escape(error)}</p>' if error else ""
    who_html = ""
    if context is not None:
        # Everything client-controlled (name, id, redirect host) is escaped —
        # a DCR-registered client_name must not become markup on OUR page.
        who_html = _WHO.format(
            client_name=html.escape(context["client_name"]),
            client_id=html.escape(context["client_id"]),
            redirect_host=html.escape(context["redirect_host"]),
        )
    return HTMLResponse(
        _PAGE.format(txn=html.escape(txn), who=who_html, error=error_html),
        status_code=status,
    )


_EXPIRED = "This authorization request has expired. Start again from Claude.ai."
_CONSUMED = (
    "Too many failed attempts — this authorization request has been cancelled. "
    "Start again from Claude.ai."
)


class _FailureThrottle:
    """Per-app exponential delay on consecutive login failures; reset on
    success. State is per app instance (not module-global) so parallel test
    apps don't couple."""

    def __init__(self) -> None:
        self._consecutive = 0

    async def on_failure(self) -> None:
        self._consecutive += 1
        delay = min(
            LOGIN_FAILURE_BASE_DELAY * 2 ** (self._consecutive - 1),
            LOGIN_FAILURE_MAX_DELAY,
        )
        if delay > 0:
            await anyio.sleep(delay)

    def on_success(self) -> None:
        self._consecutive = 0


def login_routes(
    provider: RemoteFrontProvider, owner_password: str
) -> list[Route]:
    throttle = _FailureThrottle()
    owner_password_bytes = owner_password.encode("utf-8")

    async def login_get(request: Request) -> Response:
        txn = request.query_params.get("txn", "")
        context = provider.pending_login(txn) if txn else None
        if context is None:
            return _render("", error=_EXPIRED, status=400)
        return _render(txn, context=context)

    async def login_post(request: Request) -> Response:
        form = await request.form()
        txn = str(form.get("txn", ""))
        password = str(form.get("password", ""))
        context = provider.pending_login(txn) if txn else None
        if context is None:
            return _render("", error=_EXPIRED, status=400)
        # Constant-time compare over UTF-8 BYTES (compare_digest raises
        # TypeError on non-ASCII str) so a wrong password can't be timing-probed
        # and a non-ASCII guess can't 500.
        if not hmac.compare_digest(password.encode("utf-8"), owner_password_bytes):
            still_usable = provider.fail_login(txn)
            await throttle.on_failure()
            if not still_usable:
                return _render("", error=_CONSUMED, status=400)
            return _render(
                txn, context=context, error="Incorrect credential.", status=401
            )
        throttle.on_success()
        redirect_url = provider.complete_login(txn)
        return RedirectResponse(
            url=redirect_url, status_code=302, headers={"Cache-Control": "no-store"}
        )

    return [
        Route("/login", endpoint=login_get, methods=["GET"]),
        Route("/login", endpoint=login_post, methods=["POST"]),
    ]
