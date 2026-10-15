"""Phase 2 additive backend core (core security).

Revision ID: 20261015_05
Revises: 20261015_04
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261015_05"
down_revision = "20261015_04"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "005_core_security.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    # Destructive rollback of identities/sessions and audit history is not safe.
    raise RuntimeError("Phase 2 core migrations are forward-only; restore from backup")
