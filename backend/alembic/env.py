"""Alembic runner. Hand-authored SQL migrations are the authoritative DDL."""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# This project uses SQL-first migrations (partitioning and RLS cannot be autogen'd safely).
target_metadata = None


def get_url() -> str:
    url = os.environ.get("DATABASE_ADMIN_URL")
    if not url or not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("DATABASE_ADMIN_URL must be a postgresql+psycopg:// admin URL")
    return url


def run_migrations_offline() -> None:
    context.configure(url=get_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
