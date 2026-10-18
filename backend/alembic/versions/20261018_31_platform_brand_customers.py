"""Platform branding and administrative customer directory.

Revision ID: 20261018_31
Revises: 20261018_30
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261018_31"
down_revision = "20261018_30"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "031_platform_brand_customers.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Platform branding/customer directory is forward-only; restore from backup")
