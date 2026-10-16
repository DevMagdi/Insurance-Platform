"""Add the first-party, permission-gated storage inventory capability.

Revision ID: 20261016_23
Revises: 20261016_22
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261016_23"
down_revision = "20261016_22"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "023_storage_console.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Storage console migration is forward-only; restore from backup")
