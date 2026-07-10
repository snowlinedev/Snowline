"""Deploy entrypoint: `snowline-remote-front` (or `python -m snowline_remote_front`).

Binds 0.0.0.0 on the container port — the fly proxy terminates public TLS in
front of it, and tailscaled (the sidecar) owns the OUTBOUND tailnet path to the
upstream gateway. See docs/ops/remote-front-runbook.md. The app itself imports
neither fly nor tailscale; this module is the only place a port/host is chosen.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from snowline_remote_front.app import create_app
from snowline_remote_front.config import Config


def main() -> None:
    logging.basicConfig(level=os.environ.get("REMOTE_FRONT_LOG_LEVEL", "INFO"))
    config = Config.from_env()
    app = create_app(config)
    uvicorn.run(
        app,
        host=os.environ.get("REMOTE_FRONT_HOST", "0.0.0.0"),
        port=int(os.environ.get("REMOTE_FRONT_PORT", "8080")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
