"""Partitioned complaint schema and access-control tables.

Revision ID: 20261015_01
Revises:
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261015_01"
down_revision = None
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "001_schema.sql"


def upgrade() -> None:
    for statement in sqlparse.split(SQL.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    # Destructive downgrade, only for disposable development databases.
    for name in (
        "audit_logs",
        "export_jobs",
        "report_schedules",
        "assignment_rules",
        "csat_responses",
        "duplicate_candidates",
        "ai_insights",
        "call_logs",
        "api_keys",
        "webhooks",
        "integrations_config",
        "saved_filters",
        "canned_replies",
        "sla_rules",
        "notifications",
        "attachments",
        "complaint_messages",
        "complaint_events",
        "complaints",
        "complaint_keys",
        "categories",
        "policies",
        "access_change_log",
        "user_ownership_level",
        "user_insurance_type_scope",
        "user_company_scope",
        "team_members",
        "teams",
        "user_permission_overrides",
        "user_roles",
        "role_permissions",
        "roles",
        "permissions",
        "staff_users",
        "customers",
        "insurance_companies",
    ):
        op.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
    op.execute("DROP SEQUENCE IF EXISTS complaint_ticket_sequence")
    for name in (
        "attachment_scan_status",
        "message_direction",
        "communication_channel",
        "message_visibility",
        "ownership_level",
        "permission_effect",
        "staff_status",
        "complaint_priority",
        "complaint_status",
        "insurance_type",
    ):
        op.execute(f"DROP TYPE IF EXISTS {name}")
    op.execute("DROP SCHEMA IF EXISTS app_security CASCADE")
