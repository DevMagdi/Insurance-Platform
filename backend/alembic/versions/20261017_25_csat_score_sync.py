"""Project submitted CSAT scores onto complaints.csat_score.

Revision ID: 20261017_25
Revises: 20261016_24
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261017_25"
down_revision = "20261016_24"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "025_csat_score_sync.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("CSAT score sync is forward-only; restore from backup")
