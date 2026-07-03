"""Read-only HTTP view of the gateway's mounted surfaces (ui-shell.md §6).

`GET /surfaces` reports, for each mounted named surface: its route, its plugin
allowlist (`"*"` for allow-all), and the plugins currently composed onto it —
the same `discover_upstreams` the gateway aggregates with, so the view can
never drift from what the gateway actually serves. Rides behind the trust
middleware like every platform route; the dashboard's Surfaces page is the
consumer.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from snowline_platform import config
from snowline_platform.gateway import discover_upstreams
from snowline_platform.gateway_app import surface_route

router = APIRouter(prefix="/surfaces", tags=["surfaces"])


@router.get("")
async def list_surfaces(request: Request) -> dict:
    registry = request.app.state.registry
    allowlists = config.surface_plugins()
    surfaces = []
    for name in config.surfaces():
        allow = allowlists.get(name)
        upstreams = discover_upstreams(registry, name, allowlist=allow)
        surfaces.append(
            {
                "name": name,
                "route": surface_route(name),
                "allowlist": sorted(allow) if allow is not None else "*",
                "plugins": sorted({u.plugin_name for u in upstreams}),
            }
        )
    return {"surfaces": surfaces}
