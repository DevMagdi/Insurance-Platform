"""Reauthorized worker-only exports and download manifest.

Revision ID: 20261015_11
Revises: 20261015_10
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261015_11"
down_revision = "20261015_10"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "011_async_exports.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Phase 3 async exports migration is forward-only; restore from backup")
