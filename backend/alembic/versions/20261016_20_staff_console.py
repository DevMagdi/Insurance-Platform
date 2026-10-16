"""Scope-safe staff console metadata, teams and automation administration.

Revision ID: 20261016_20
Revises: 20261016_19
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261016_20"
down_revision = "20261016_19"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "020_staff_console.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Phase 7 staff console migration is forward-only; restore from backup")
