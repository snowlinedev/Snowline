from alembic import context
from sqlalchemy import engine_from_config, pool

from snowline_platform.config import database_url
from snowline_platform.models import Base
from snowline_plugin_sdk.replication.models import ReplicationBase

config = context.config
config.set_main_option("sqlalchemy.url", database_url())

# Two metadatas: the platform's own `Scope` table, and the SDK's replication
# tables the platform adopts (spec §8, issue #81) — a SEPARATE metadata by
# design (SDK `replication/models.py`), so autogenerate needs both named here.
target_metadata = [Base.metadata, ReplicationBase.metadata]


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
