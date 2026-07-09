"""The single-user login step.

Claude.ai opens the /authorize URL in the resource owner's browser; the SDK
authorize handler (via `RemoteFrontProvider.authorize`) redirects here. This page
is the ONLY human touch point: one password field checked against the fixed
resource-owner credential. On success we mint the authorization code and 302 back
to the client's redirect_uri; there is no user database and no session cookie —
the transaction id in the URL carries the parked authorization.
"""

from __future__ import annotations

import hmac

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from snowline_remote_front.provider import RemoteFrontProvider

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Snowline remote access</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 22rem; margin: 4rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.1rem; }}
  input, button {{ font-size: 1rem; padding: .5rem; width: 100%; box-sizing: border-box; }}
  button {{ margin-top: .75rem; cursor: pointer; }}
  .err {{ color: #b00020; }}
</style></head>
<body>
  <h1>Snowline remote access</h1>
  <p>Authorize this Claude.ai connector to reach your Snowline surface.</p>
  {error}
  <form method="post" action="/login">
    <input type="hidden" name="txn" value="{txn}">
    <input type="password" name="password" placeholder="Resource-owner credential" autofocus autocomplete="current-password">
    <button type="submit">Authorize</button>
  </form>
</body></html>"""


def _render(txn: str, *, error: str | None = None, status: int = 200) -> HTMLResponse:
    error_html = f'<p class="err">{error}</p>' if error else ""
    return HTMLResponse(
        _PAGE.format(txn=txn, error=error_html), status_code=status
    )


def login_routes(
    provider: RemoteFrontProvider, owner_password: str
) -> list[Route]:
    async def login_get(request: Request) -> Response:
        txn = request.query_params.get("txn", "")
        if not txn or not provider.pending_login(txn):
            return _render(
                "", error="This authorization request has expired. Start again from Claude.ai.", status=400
            )
        return _render(txn)

    async def login_post(request: Request) -> Response:
        form = await request.form()
        txn = str(form.get("txn", ""))
        password = str(form.get("password", ""))
        if not txn or not provider.pending_login(txn):
            return _render(
                "", error="This authorization request has expired. Start again from Claude.ai.", status=400
            )
        # Constant-time compare so a wrong password can't be timing-probed.
        if not hmac.compare_digest(password, owner_password):
            return _render(txn, error="Incorrect credential.", status=401)
        redirect_url = provider.complete_login(txn)
        return RedirectResponse(
            url=redirect_url, status_code=302, headers={"Cache-Control": "no-store"}
        )

    return [
        Route("/login", endpoint=login_get, methods=["GET"]),
        Route("/login", endpoint=login_post, methods=["POST"]),
    ]
