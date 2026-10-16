"""Statement-level (InitPlan) RLS privilege evaluation for complaints.

Revision ID: 20261016_22
Revises: 20261016_21
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261016_22"
down_revision = "20261016_21"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "022_rls_scope_initplans.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Phase 8 RLS migration is forward-only; restore from backup")
