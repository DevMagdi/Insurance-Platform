"""Assignment rules, integration settings and duplicate detection.

Revision ID: 20261015_14
Revises: 20261015_13
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261015_14"
down_revision = "20261015_13"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "014_ops_admin.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError(
        "Phase 3 operations administration migration is forward-only; restore from backup"
    )
