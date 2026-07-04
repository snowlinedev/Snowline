"""Test utilities for the registration heartbeat (issue #50).

`run_heartbeat_until` is the ~30-line async harness that had been copy-pasted
into `governance/tests/` and `memory/tests/`: it runs a heartbeat loop against a
stubbed platform (an `httpx.MockTransport`, so no platform process runs) until a
target number of POSTs have landed, then cancels it — exactly how the app
lifespan tears the loop down. Both the SDK's own registration tests and the
plugins' plugin-specific tests import it instead of redefining it.

This module lives in the SDK's `[client]` extra (it imports `httpx`/`anyio`) and
is a TEST helper — importing it is never part of a plugin's runtime path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import anyio
import httpx


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """An `httpx.Client` whose transport is `handler` — no platform process runs."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def run_heartbeat_until(
    heartbeat: Callable[..., Awaitable[None]],
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    beats: int,
    timeout: float = 5.0,
    **heartbeat_kwargs,
) -> int:
    """Run `heartbeat` (a coroutine function — the SDK's `registration_heartbeat`
    or a plugin wrapper of it) against a stubbed platform until `beats` POSTs have
    landed, then cancel it. `heartbeat` is invoked with a `client=` wired to a
    counting `MockTransport` plus any `heartbeat_kwargs` (e.g. `interval=0.01`,
    and for the raw SDK call `manifest_builder=`, `platform_url=`, `plugin_name=`,
    `log=`). Returns the POST count observed."""
    count = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return handler(request)

    async def main():
        async with anyio.create_task_group() as tg:

            async def _beat():
                await heartbeat(client=mock_client(counting_handler), **heartbeat_kwargs)

            tg.start_soon(_beat)
            with anyio.fail_after(timeout):
                while count < beats:
                    await anyio.sleep(0.005)
            tg.cancel_scope.cancel()

    anyio.run(main)
    return count
