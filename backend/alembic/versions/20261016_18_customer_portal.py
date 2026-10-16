"""Customer portal profile, notification preferences and realtime helpers.

Revision ID: 20261016_18
Revises: 20261015_17
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261016_18"
down_revision = "20261015_17"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "018_customer_portal.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Phase 6 customer portal migration is forward-only; restore from backup")
