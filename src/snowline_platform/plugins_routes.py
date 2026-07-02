"""HTTP surface for the plugin registry.

`POST /plugins` registers (an idempotent UPSERT — it is the registration
heartbeat's verb, issue #39), `GET /plugins` lists, `DELETE /plugins/{name}`
unregisters. These ride behind the platform's trust middleware, so only a
trusted principal can change the plugin set — registration trust is the platform's
access gate (tailnet membership today, OAuth later).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import (
    PluginNotFound,
    PluginRegistry,
    RegisteredPlugin,
)

router = APIRouter(prefix="/plugins", tags=["plugins"])


def _entry_dict(entry: RegisteredPlugin) -> dict:
    return {
        "name": entry.manifest.name,
        "status": entry.status.value,
        "manifest": entry.manifest.model_dump(),
    }


def _registry(request: Request) -> PluginRegistry:
    return request.app.state.registry


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_plugin(
    manifest: PluginManifest, request: Request, response: Response
) -> dict:
    """Register a plugin — an idempotent UPSERT, so the plugin-side registration
    heartbeat (issue #39) can re-POST the same manifest every beat: 201 on first
    registration, 200 when already known (identical manifest keeps the entry and
    its health status; a changed manifest replaces it — a redeploy that moved or
    re-shaped a plugin takes effect without an unregister)."""
    entry, outcome = _registry(request).upsert(manifest)
    if outcome != "created":
        response.status_code = status.HTTP_200_OK
    return _entry_dict(entry) | {"outcome": outcome}


@router.get("")
async def list_plugins(request: Request) -> dict:
    return {"plugins": [_entry_dict(e) for e in _registry(request).list()]}


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_plugin(name: str, request: Request) -> None:
    try:
        _registry(request).unregister(name)
    except PluginNotFound:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"plugin {name!r} not found"
        ) from None
