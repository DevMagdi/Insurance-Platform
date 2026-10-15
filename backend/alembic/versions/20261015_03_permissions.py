"""Install the immutable permission catalog independent of the optional demo seed.

Revision ID: 20261015_03
Revises: 20261015_02
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "20261015_03"
down_revision = "20261015_02"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "003_permissions.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    # Catalog entries can be referenced by user and role grants. A downgrade
    # should not silently remove operator-defined access; keep catalog rows.
    pass
