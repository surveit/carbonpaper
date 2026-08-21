"""Alembic entry point. See docs/models-and-storage.md."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from app.core.store_config import create_db_directory, refuse_renamed_env_vars
from app.core.table_spec import METADATA

# Importing a columnized record is what puts its table in METADATA, so autogenerate
# cannot see a record no module here reaches.
import app.core.files  # noqa: F401

target_metadata = METADATA


def run_migrations_online() -> None:
    engine = create_engine(f"sqlite+pysqlite:///{create_db_directory()}")
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=f"sqlite+pysqlite:///{create_db_directory()}",
                      target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


refuse_renamed_env_vars()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
