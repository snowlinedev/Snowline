from alembic import context
from snowline_plugin_sdk.replication.models import ReplicationBase
from sqlalchemy import engine_from_config, pool

from snowline_memory.config import database_url
from snowline_memory.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", database_url())

# Both memory's domain metadata AND the SDK-owned replication metadata (adopted
# in the c3d4e5f6a7b8 migration, #80) — so an autogenerate diff sees the
# replication tables as OWNED, not as strays to drop.
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
