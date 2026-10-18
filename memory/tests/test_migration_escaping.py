"""Percent-encoded DB credentials must not break migrations/env.py (Snowline#217)."""

from pathlib import Path

import snowline_memory
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from snowline_memory.config import escape_for_config


def test_env_cli_fallback_survives_percent_in_url(monkeypatch, capsys):
    # Offline (`sql=True`) mode runs env.py without connecting, exercising the
    # CLI-fallback `set_main_option` that raised ValueError before the fix.
    monkeypatch.setenv(
        "SNOWLINE_MEMORY_DATABASE_URL", "postgresql+psycopg://user:p%40ss@localhost/db"
    )
    migrations = Path(snowline_memory.__file__).resolve().parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", escape_for_config(str(migrations)))
    # Render only the first revision: later data migrations query the DB, which
    # offline mode cannot answer. env.py runs (and must not raise) regardless.
    first = ScriptDirectory.from_config(cfg).get_base()
    command.upgrade(cfg, f"base:{first}", sql=True)
    assert "CREATE TABLE" in capsys.readouterr().out


def test_escape_round_trips_through_config():
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", escape_for_config("postgresql://u:p%40@h/d"))
    assert cfg.get_main_option("sqlalchemy.url") == "postgresql://u:p%40@h/d"
