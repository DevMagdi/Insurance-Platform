"""Real provider connections: status tracking, OAuth email, SMS, speech, ClamAV.

Adds the connection-test lifecycle on ``integrations_config``, the Gmail and
Outlook OAuth transports, Twilio SMS with delivery callbacks, the OpenAI
speech adapters, ClamAV scanning and the ``sms`` message channel. The old
``secret_ref`` vault-name functions are dropped: credentials live only in the
AES-GCM ``config_ciphertext`` column managed by ``save_integration_config``.

Revision ID: 20261017_28
Revises: 20261017_27
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261017_28"
down_revision = "20261017_27"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "028_integration_connections.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Integration connections are forward-only; restore from backup")
