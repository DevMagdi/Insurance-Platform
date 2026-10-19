"""Staff notices for customer activity and live directory revision signatures.

Revision ID: 20261019_35
Revises: 20261019_34
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261019_35"
down_revision = "20261019_34"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "035_staff_notices_live_directory.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError(
        "Staff notices and directory revisions are forward-only; restore from backup"
    )
