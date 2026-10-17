"""Repair outbound integration-job enqueue (ambiguous ``state`` reference).

Revision ID: 20261017_27
Revises: 20261017_26
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261017_27"
down_revision = "20261017_26"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "027_integration_job_enqueue.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Integration enqueue repair is forward-only; restore from backup")
