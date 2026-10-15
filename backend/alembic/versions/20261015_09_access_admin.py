"""Guarded role and staff lifecycle administration.

Revision ID: 20261015_09
Revises: 20261015_08
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261015_09"
down_revision = "20261015_08"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "009_access_admin.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Phase 3 access admin migration is forward-only; restore from backup")
