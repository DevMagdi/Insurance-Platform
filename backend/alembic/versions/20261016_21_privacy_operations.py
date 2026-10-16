"""PDPL consent evidence, privacy requests, retention and queue telemetry.

Revision ID: 20261016_21
Revises: 20261016_20
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261016_21"
down_revision = "20261016_20"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "021_privacy_operations.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Phase 8 privacy migration is forward-only; restore from backup")
