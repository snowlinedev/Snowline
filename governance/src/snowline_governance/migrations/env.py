from alembic import context
from sqlalchemy import engine_from_config, pool
from snowline_plugin_sdk.replication.models import ReplicationBase

from snowline_governance.config import database_url
from snowline_governance.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", database_url())

# Governance's own metadata PLUS the adopted SDK replication tables
# (replication-continuity §4, #79) — so autogenerate compares against the full
# set this chain owns instead of proposing drops of the SDK tables.
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
