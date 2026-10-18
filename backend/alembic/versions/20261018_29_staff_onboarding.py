"""Department-bound invitations and encrypted staff profiles/avatars.

Revision ID: 20261018_29
Revises: 20261017_28
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261018_29"
down_revision = "20261017_28"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "029_staff_onboarding.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Staff onboarding is forward-only; restore from backup")
