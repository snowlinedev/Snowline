"""HTTP surface for the plugin registry.

`POST /plugins` registers (an idempotent UPSERT — it is the registration
heartbeat's verb, issue #39), `GET /plugins` lists, `DELETE /plugins/{name}`
unregisters. These ride behind the platform's trust middleware, so only a
trusted principal can change the plugin set — registration trust is the platform's
access gate (tailnet membership today, OAuth later).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status

from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import (
    PluginNotFound,
    PluginRegistry,
    RegisteredPlugin,
)

log = logging.getLogger("snowline_platform.plugins")

router = APIRouter(prefix="/plugins", tags=["plugins"])


def _entry_dict(entry: RegisteredPlugin) -> dict:
    return {
        "name": entry.manifest.name,
        "status": entry.status.value,
        "manifest": entry.manifest.model_dump(),
    }


def _registry(request: Request) -> PluginRegistry:
    return request.app.state.registry


def _reject_reserved_name(name: str) -> None:
    """The `platform` entry is the platform's OWN self-registration (decision
    0503fff0), seeded at startup — the one name the external registration surface
    must not touch: a POST would hijack the `platform__*` tool namespace onto a
    foreign base_url, and a DELETE would silently drop every native tool until
    the next restart. Same reservation posture as the surface-name check in
    `create_app`."""
    from snowline_platform.platform_tools import PLATFORM_PLUGIN_NAME

    if name == PLATFORM_PLUGIN_NAME:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{name!r} is the platform's own self-entry (decision 0503fff0) — "
            "it is seeded at startup and cannot be registered or removed over "
            "this surface",
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_plugin(
    manifest: PluginManifest, request: Request, response: Response
) -> dict:
    """Register a plugin — an idempotent UPSERT, so the plugin-side registration
    heartbeat (issue #39) can re-POST the same manifest every beat: 201 on first
    registration, 200 when already known (identical manifest keeps the entry and
    its health status; a changed manifest replaces it — a redeploy that moved or
    re-shaped a plugin takes effect without an unregister)."""
    _reject_reserved_name(manifest.name)
    entry, outcome = _registry(request).upsert(manifest)
    if outcome == "created":
        log.info("plugin %r registered (base_url %s)", manifest.name, manifest.base_url)
    else:
        response.status_code = status.HTTP_200_OK
        if outcome == "updated":
            # Loud on purpose: a changed manifest is a redeploy taking effect —
            # OR two live instances fighting for one name, which shows up as
            # this line repeating every heartbeat with alternating base_urls.
            log.warning(
                "plugin %r manifest REPLACED (base_url now %s; health status "
                "reset) — if this repeats every beat, two live instances are "
                "contending for the name",
                manifest.name,
                manifest.base_url,
            )
    return _entry_dict(entry) | {"outcome": outcome}


@router.get("")
async def list_plugins(request: Request) -> dict:
    return {"plugins": [_entry_dict(e) for e in _registry(request).list()]}


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_plugin(name: str, request: Request) -> None:
    """Unregister a plugin. NOTE: a LIVE plugin's registration heartbeat will
    re-upsert it within one interval (issue #39) — to durably remove a plugin,
    stop its process first; DELETE alone only sticks for stopped plugins."""
    _reject_reserved_name(name)
    try:
        _registry(request).unregister(name)
    except PluginNotFound:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"plugin {name!r} not found"
        ) from None
