"""Customer-profile national ID reveal and virus-scanner presence probe.

Revision ID: 20261019_34
Revises: 20261019_33
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261019_34"
down_revision = "20261019_33"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "034_customer_national_id_and_scanner.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Customer national ID profile access is forward-only; restore from backup")
