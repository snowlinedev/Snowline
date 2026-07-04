"""Registering the governance plugin with the platform.

A plugin joins the platform by POSTing its manifest to `POST /plugins` (the
platform registry, architecture §2). Governance declares: its name (`governance`),
its `base_url`, its `mcp_path` (`/mcp`), its `health_path` (`/health`), and its
SURFACE MAPPING — `/mcp -> main` (the real-write decision + artifact tools + the
reads land on the platform's composed `main` surface) AND `/shadow/mcp -> shadow`
(the speculation write tools + read-real grounding, NO real-write — the isolation
property, decision 8a7f0a11).

Registration is BEST-EFFORT and RETRYABLE (architecture §3: hot-pluggable, no
platform restart). The platform may be briefly down when this plugin boots; that
must NOT crash the plugin. `register_with_platform` swallows transport errors and
returns False (logging), so the app starts regardless. The caller IS a loop:
`registration_heartbeat` re-POSTs the manifest every beat for the app's whole
lifespan (issue #39) — the platform's registry is in-memory, so a platform
restart empties it and only a re-assert from this side heals the composed
surfaces. The platform's `POST /plugins` is an idempotent upsert (200 on
re-register); a 409 from an older platform is also treated as success.

The heartbeat MECHANISM — the POST, the lazy per-loop client, the #51 hardening
(non-finite interval guard, per-beat backstop, one boot-confirmation INFO then
silent steady state, scoped httpx log filter) — lives ONCE in
`snowline_plugin_sdk.registration` (issue #50); this module is just governance's
manifest builder plus thin, plugin-labelled calls into it.
"""

from __future__ import annotations

import logging

from snowline_plugin_sdk import registration as sdk_registration

from snowline_governance import config

log = logging.getLogger("snowline_governance.registration")

PLUGIN_NAME = "governance"


def build_manifest(base_url: str | None = None) -> dict:
    """The manifest governance hands the platform. `base_url` defaults to
    `config.base_url()` (where this plugin advertises itself).

    `ui` (ui-shell.md §3, issue #55): governance is the FIRST plugin with a
    registered UI contribution — the shadow-discussions views, read-only (§8
    step 3), over the `/ui-api` routes in `ui_api.py`. One `stat` widget (open
    shadow branches, home grid) and two pages: `shadow-branches` (`table`,
    every branch across scopes) and `shadow-branch` (`thread`, one branch's
    narrative notes + nodes, reached by the table's row links — `nav: False`
    since it's not a nav destination on its own). The `shadow-branch` page's
    route/data both key on the branch's stable `id` (see `ui_api.py`'s
    module docstring for why: branch names are only unique WITHIN a scope, so
    a `<scope>:<name>` route would need a two-segment or percent-encoded
    param; `id` round-trips in one).
    """
    return {
        "name": PLUGIN_NAME,
        "base_url": (base_url or config.base_url()),
        "mcp_path": "/mcp",
        "health_path": "/health",
        # Plugin-path -> platform named-surface (gateway.md §2): the real-write
        # decision + artifact tools on `/mcp` compose onto the platform's `main`
        # surface; the speculation surface on `/shadow/mcp` (shadow writes +
        # read-real grounding, NO real-write) composes onto `shadow` — the
        # isolation mirrors the MCP isolation (decision 8a7f0a11).
        "surfaces": {"/mcp": "main", "/shadow/mcp": "shadow"},
        "ui": {
            "contract_version": 1,
            "widgets": [
                {
                    "id": "shadow-activity",
                    "slot": "home",
                    "kind": "stat",
                    "title": "Open shadow branches",
                    "data": "/ui-api/widgets/shadow-activity",
                    "refresh_seconds": 30,
                },
            ],
            "pages": [
                {
                    "id": "shadow-branches",
                    "route": "/shadow",
                    "title": "Shadow discussions",
                    "nav": True,
                    "kind": "table",
                    "data": "/ui-api/pages/branches",
                },
                {
                    "id": "shadow-branch",
                    "route": "/shadow/{branch_id}",
                    "nav": False,
                    "kind": "thread",
                    "data": "/ui-api/pages/branches/{branch_id}",
                },
            ],
        },
    }


def register_with_platform(
    platform_url: str | None = None,
    base_url: str | None = None,
    *,
    client=None,
    timeout: float = 10.0,
) -> bool:
    """POST governance's manifest to the platform's `POST /plugins` — a thin,
    plugin-labelled call into the shared SDK client. Best-effort (never raises);
    see `snowline_plugin_sdk.registration.register_with_platform` for the
    idempotent-upsert / 409-as-success / transport-error semantics."""
    return sdk_registration.register_with_platform(
        build_manifest(base_url),
        platform_url or config.platform_url(),
        plugin_name=PLUGIN_NAME,
        log=log,
        client=client,
        timeout=timeout,
    )


async def registration_heartbeat(
    platform_url: str | None = None,
    base_url: str | None = None,
    *,
    interval: float | None = None,
    client=None,
) -> None:
    """Re-assert governance's registration on the shared heartbeat (issue #39) —
    a thin call into `snowline_plugin_sdk.registration.registration_heartbeat`,
    which owns the beat-on-boot / lazy per-loop client / #51 hardening. `interval`
    defaults to `config.registration_heartbeat_seconds()` (the shared lenient
    parse); the manifest is rebuilt each beat so a config change is picked up."""
    await sdk_registration.registration_heartbeat(
        lambda: build_manifest(base_url),
        platform_url or config.platform_url(),
        plugin_name=PLUGIN_NAME,
        log=log,
        interval=(
            interval
            if interval is not None
            else config.registration_heartbeat_seconds()
        ),
        client=client,
    )
