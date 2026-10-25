"""Isolated CRM module: crm_leads, import batches/errors, activities.

Revision ID: 20261024_40
Revises: 20261023_39
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261024_40"
down_revision = "20261023_39"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "040_crm_module.sql"


def _statements(sql: str) -> list[str]:
    return [
        statement
        for statement in sqlparse.split(sql)
        if "\n".join(
            line for line in statement.splitlines() if not line.strip().startswith("--")
        ).strip(" ;\n\t")
    ]


def upgrade() -> None:
    for statement in _statements(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    """Remove only this module's tables; no existing table is touched."""
    for table in ("crm_activities", "crm_import_errors", "crm_leads", "crm_import_batches"):
        op.execute(f"DROP TABLE IF EXISTS public.{table}")
