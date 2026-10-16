"""CSV, Excel and PDF export formats.

Revision ID: 20261015_16
Revises: 20261015_15
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261015_16"
down_revision = "20261015_15"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "016_export_formats.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Phase 3 export formats migration is forward-only; restore from backup")
