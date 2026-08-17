"""Alembic entry point. Migrates the document store's JSON payloads, not a
relational schema: there is no ORM metadata and autogenerate is not used, so
`target_metadata` stays None and every revision is hand-written."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from app.core.store_config import create_db_directory, refuse_renamed_env_vars

target_metadata = None


def run_migrations_online() -> None:
    engine = create_engine(f"sqlite+pysqlite:///{create_db_directory()}")
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=f"sqlite+pysqlite:///{create_db_directory()}",
                      target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


refuse_renamed_env_vars()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
