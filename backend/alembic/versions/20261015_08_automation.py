"""Phase 3 opt-in auto-assignment and restricted SLA/insurer delivery jobs.

Revision ID: 20261015_08
Revises: 20261015_07
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261015_08"
down_revision = "20261015_07"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "008_automation.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Phase 3 automation migration is forward-only; restore from backup")
