"""Fail-closed RLS, scope checks and restricted runtime grants.

Revision ID: 20261015_02
Revises: 20261015_01
"""

from __future__ import annotations

from pathlib import Path

import sqlparse
from sqlalchemy import text

from alembic import op

revision = "20261015_02"
down_revision = "20261015_01"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "002_access_rls.sql"
RLS_TABLES = (
    "complaints",
    "complaint_keys",
    "complaint_events",
    "complaint_messages",
    "attachments",
    "notifications",
    "call_logs",
    "audit_logs",
    "access_change_log",
    "customers",
    "policies",
    "categories",
    "sla_rules",
    "canned_replies",
    "ai_insights",
    "duplicate_candidates",
    "csat_responses",
    "report_schedules",
    "export_jobs",
    "assignment_rules",
)


def upgrade() -> None:
    migrator_bypasses_rls = (
        op.get_bind()
        .execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"))
        .scalar_one()
    )
    if not migrator_bypasses_rls:
        raise RuntimeError("RLS SECURITY DEFINER functions require a trusted BYPASSRLS migrator")
    role = (
        op.get_bind()
        .execute(
            text(
                "SELECT rolcanlogin AND NOT rolbypassrls AND NOT rolsuper "
                "FROM pg_roles WHERE rolname = 'app_runtime'"
            )
        )
        .scalar_one_or_none()
    )
    if role is not True:
        raise RuntimeError("Provision a non-superuser/non-BYPASSRLS app_runtime role first")
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_complaints_policy")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_runtime")
    op.execute("REVOKE ALL ON SCHEMA app_security FROM app_runtime")
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE IF EXISTS {table} DISABLE ROW LEVEL SECURITY")
    # Dropping the functions cascades only policies from this revision. Recreate
    # the empty schema to preserve the 001 migration's schema contract.
    op.execute("DROP SCHEMA app_security CASCADE")
    op.execute("CREATE SCHEMA app_security")
