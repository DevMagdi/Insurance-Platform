"""Link complaint customers to policies and historical companies.

Revision ID: 20261019_33
Revises: 20261018_32
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261019_33"
down_revision = "20261018_32"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "033_customer_policy_link.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Customer policy linking is forward-only; restore from backup")
