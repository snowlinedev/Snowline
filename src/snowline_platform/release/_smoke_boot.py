"""The wheel-boot smoke driver (macOS distribution spec §2.1, risk #1).

This file is NOT imported by the platform. It is copied into a throwaway venv
built from the release wheels and run by *that* venv's interpreter, so it may
only use the standard library plus what every Snowline service already depends
on at runtime (alembic, sqlalchemy). It lives as a real module rather than a
string inside `cut.py` so it is lintable and readable, and it ships in the
wheel so the cutter can find it next to itself however the platform was
installed.

    python _smoke_boot.py '<json spec>'

The spec names the app module, how to construct the app, and the database URL
env var. What it proves, per §2.1: a service installed **from its wheel**,
with no checkout anywhere on the path, boots against an empty database and
migrates it to head.

Boot goes through the ASGI lifespan protocol by hand rather than through
uvicorn or a test client — the lifespan is exactly what runs the boot-migrate
(the `schema-pr-deploy-needs-live-db-migration` posture), and driving it
directly needs no HTTP stack, no port, and no readiness polling.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path


def _build_app(spec: dict):
    module = importlib.import_module(spec["module"])
    if spec.get("factory"):
        return getattr(module, spec["factory"])(**spec.get("kwargs", {}))
    return getattr(module, spec["attr"])


async def _drive_lifespan(app) -> None:
    """Run startup, then shutdown, asserting startup completed."""
    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "lifespan.startup"})
    seen: list[dict] = []

    async def receive():
        return await inbox.get()

    async def send(message):
        seen.append(message)
        kind = message["type"]
        if kind == "lifespan.startup.failed":
            raise RuntimeError(f"startup failed: {message.get('message', '')}")
        if kind == "lifespan.startup.complete":
            await inbox.put({"type": "lifespan.shutdown"})

    await app({"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}, receive, send)
    kinds = [m["type"] for m in seen]
    if "lifespan.startup.complete" not in kinds:
        raise RuntimeError(f"lifespan never completed startup: {kinds}")


def _assert_at_head(spec: dict, url: str) -> list[str]:
    """Compare the DB's stamped revision(s) against the package's alembic heads.

    The migrations directory is resolved from the INSTALLED package
    (`<package>/migrations`), which is the same package-internal resolution the
    services' boot-migrate uses — so this asserts against the wheel's own
    migration chain, not a checkout's.
    """
    import sqlalchemy as sa
    from alembic.script import ScriptDirectory

    module = importlib.import_module(spec["module"])
    root = Path(module.__file__).resolve().parent
    migrations = root / "migrations"
    if not migrations.exists():
        # Some packages nest the app module a level down; walk up one.
        migrations = root.parent / "migrations"
    if not migrations.exists():
        raise RuntimeError(f"no package-internal migrations directory under {root}")
    heads = set(ScriptDirectory(str(migrations)).get_heads())

    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text("select version_num from alembic_version")).scalars().all()
    finally:
        engine.dispose()
    stamped = set(rows)
    if not stamped:
        raise RuntimeError("alembic_version is empty — boot-migrate did not run")
    if stamped != heads:
        raise RuntimeError(f"database at {sorted(stamped)}, package heads are {sorted(heads)}")
    return sorted(stamped)


def main(argv: list[str]) -> int:
    spec = json.loads(argv[1])
    url = os.environ[spec["database_url_env"]]
    app = _build_app(spec)
    asyncio.run(_drive_lifespan(app))
    heads = _assert_at_head(spec, url)
    print(f"smoke ok: {spec['module']} migrated to {', '.join(heads)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
