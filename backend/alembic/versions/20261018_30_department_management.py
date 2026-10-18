"""Manage department descriptions and availability without changing staff authorization.

Revision ID: 20261018_30
Revises: 20261018_29
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261018_30"
down_revision = "20261018_29"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "030_department_management.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Department management is forward-only; restore from backup")
