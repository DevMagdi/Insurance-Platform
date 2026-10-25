"""Phase 1 completions + Phase 2: multi-tenancy, Sales CRM, issuing, policies.

Revision ID: 20261023_39
Revises: 20261022_38
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261023_39"
down_revision = "20261022_38"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "039_phase12_crm_policies.sql"


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
    raise RuntimeError("Phase 1/2 CRM and policy lifecycle is forward-only; restore from backup")
