"""Grant the runtime role read access to the customer voice-consent columns.

Revision ID: 20261017_26
Revises: 20261017_25
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261017_26"
down_revision = "20261017_25"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "026_voice_consent_grant.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT (voice_followup_opt_in, voice_consent_at) "
        "ON public.customers FROM app_runtime"
    )
