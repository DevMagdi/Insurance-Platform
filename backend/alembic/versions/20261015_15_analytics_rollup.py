"""Analytics daily rollup materialized view.

Revision ID: 20261015_15
Revises: 20261015_14
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261015_15"
down_revision = "20261015_14"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "015_analytics_rollup.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Phase 3 analytics rollup migration is forward-only; restore from backup")
