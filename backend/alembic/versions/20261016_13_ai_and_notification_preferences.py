"""AI insight writes and notification-preference-aware delivery.

Revision ID: 20261016_13
Revises: 20261015_12
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261016_13"
down_revision = "20261015_12"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "013_ai_and_notification_preferences.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("AI/notification-preference migration is forward-only; restore from backup")
