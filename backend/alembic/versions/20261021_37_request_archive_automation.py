"""The request archive and the automation ledger.

Revision ID: 20261021_37
Revises: 20261021_36
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261021_37"
down_revision = "20261021_36"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "037_request_archive_automation.sql"


def _statements(sql: str) -> list[str]:
    return [
        statement
        for statement in sqlparse.split(sql)
        # sqlparse attaches leading comments to the next statement, so a chunk
        # can start with "--" and still contain real DDL.
        if "\n".join(
            line for line in statement.splitlines() if not line.strip().startswith("--")
        ).strip(" ;\n\t")
    ]


def upgrade() -> None:
    for statement in _statements(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("The archive and automation ledger are forward-only; restore from backup")
