"""Phase 2 additive backend core (core workflows).

Revision ID: 20261015_06
Revises: 20261015_05
"""

from __future__ import annotations

import os
from pathlib import Path

import sqlparse
from psycopg import sql

from alembic import op

revision = "20261015_06"
down_revision = "20261015_05"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "006_core_workflows.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)
    # Only the internal worker can execute the recipient/scan functions. No
    # password is embedded in migrations or printed in SQLAlchemy logs.
    password = os.getenv("APP_WORKER_DB_PASSWORD")
    if password:
        if len(password) < 32:
            raise RuntimeError("APP_WORKER_DB_PASSWORD must be a generated 32+ character secret")
        connection = op.get_bind().connection.driver_connection
        connection.execute(
            sql.SQL("ALTER ROLE app_worker LOGIN PASSWORD {}").format(sql.Literal(password))
        )


def downgrade() -> None:
    # Destructive rollback of identities/sessions and audit history is not safe.
    raise RuntimeError("Phase 2 core migrations are forward-only; restore from backup")
