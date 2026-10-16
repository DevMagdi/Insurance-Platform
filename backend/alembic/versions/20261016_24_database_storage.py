"""Store private binaries in PostgreSQL instead of an object store.

Revision ID: 20261016_24
Revises: 20261016_23
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261016_24"
down_revision = "20261016_23"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "024_database_storage.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Database storage migration is forward-only; restore from backup")
