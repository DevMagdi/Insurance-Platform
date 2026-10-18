"""Deliver mobile-browser staff login OTPs to encrypted staff phones.

Revision ID: 20261018_32
Revises: 20261018_31
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261018_32"
down_revision = "20261018_31"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "032_staff_mobile_otp.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Staff mobile OTP is forward-only; restore from backup")
