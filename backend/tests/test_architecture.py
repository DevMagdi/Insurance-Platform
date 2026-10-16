"""Fail-fast checks on permissions, immutable migrations and API contracts."""

from __future__ import annotations

import random
import re
from datetime import date
from pathlib import Path

import pytest
import sqlparse
import yaml
from httpx import ASGITransport, AsyncClient
from openapi_spec_validator import validate

from app.db import make_engine
from app.domain.permissions import PERMISSIONS, permission_parts
from app.main import app
from app.models import Base
from app.models.access import EFFECT_ENUM, OWNERSHIP_ENUM, STAFF_ENUM
from app.models.identity import INSURANCE_ENUM
from app.models.tickets import (
    CHANNEL_ENUM,
    DIRECTION_ENUM,
    PRIORITY_ENUM,
    SCAN_ENUM,
    STATUS_ENUM,
    VISIBILITY_ENUM,
)
from app.scripts.partitions import add_months
from app.scripts.seed import synthetic_national_id
from app.security.national_id import validate_national_id

ROOT = Path(__file__).resolve().parents[2]


async def test_liveness_and_legacy_v1_contract_is_not_implemented() -> None:
    # ASGITransport does not start lifespan; liveness has no DB/Redis dependency.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/health/live")).json() == {"status": "ok"}
        assert (await client.get("/api/v1/staff/complaints")).status_code == 404
        assert "/api/staff/complaints" in app.openapi()["paths"]


def test_catalog_has_every_required_resource_action_and_frozen_migration() -> None:
    required = {
        "complaints.view",
        "complaints.create",
        "complaints.edit",
        "complaints.assign",
        "complaints.reassign",
        "complaints.change_status",
        "complaints.reply_public",
        "complaints.add_internal_note",
        "complaints.merge",
        "complaints.escalate",
        "complaints.delete",
        "complaints.export",
        "complaints.view_attachments",
        "complaints.view_full_national_id",
        "analytics.view",
        "analytics.export",
        "users.view",
        "users.create",
        "users.edit",
        "users.deactivate",
        "users.manage_access",
        "roles.manage",
        "companies.manage",
        "categories.manage",
        "sla.manage",
        "templates.manage",
        "integrations.manage",
        "integrations.send_email",
        "integrations.send_whatsapp",
        "integrations.ai_call",
        "reports.manage",
        "audit.view",
        "settings.manage",
    }
    assert required <= PERMISSIONS
    assert permission_parts("complaints.view") == ("complaints", "view")
    with pytest.raises(ValueError):
        permission_parts("not.a.permission")
    sql = (ROOT / "backend/alembic/sql/003_permissions.sql").read_text()
    seeded_permissions = set(re.findall(r"\('([^']+)',", sql))
    phase7 = (ROOT / "backend/alembic/sql/012_staff_console.sql").read_text()
    assert seeded_permissions | {"teams.manage"} == PERMISSIONS
    assert "VALUES ('teams.manage', 'teams', 'manage'" in phase7


def test_sql_migrations_cover_every_requested_table_and_fail_closed_rls() -> None:
    schema = (ROOT / "backend/alembic/sql/001_schema.sql").read_text()
    rls = (ROOT / "backend/alembic/sql/002_access_rls.sql").read_text()
    rls += (ROOT / "backend/alembic/sql/011_async_exports.sql").read_text()
    assert len(sqlparse.split(schema)) >= 70  # PL/pgSQL bodies must not be split
    assert len(sqlparse.split(rls)) >= 60
    assert "GREATEST(6, length(seq.n::text))" in rls  # sequence must not truncate at 1M
    assert "ck_ticket_year" in schema
    required = (
        "customers",
        "insurance_companies",
        "policies",
        "complaints",
        "complaint_events",
        "complaint_messages",
        "attachments",
        "notifications",
        "sla_rules",
        "canned_replies",
        "categories",
        "saved_filters",
        "integrations_config",
        "webhooks",
        "api_keys",
        "audit_logs",
        "call_logs",
        "staff_users",
        "permissions",
        "roles",
        "role_permissions",
        "user_roles",
        "user_permission_overrides",
        "user_company_scope",
        "user_insurance_type_scope",
        "user_ownership_level",
        "teams",
        "team_members",
        "access_change_log",
    )
    for table in required:
        assert re.search(rf"CREATE TABLE {table} \(", schema), table
    # SQL-first migrations and typed SQLAlchemy mappings must evolve together.
    additive = "\n".join(
        (ROOT / "backend/alembic/sql" / revision).read_text()
        for revision in ("004_core_tables.sql", "007_staff_workflow.sql", "011_async_exports.sql")
    )
    declared_tables = set(re.findall(r"CREATE TABLE (\w+) \(", schema + additive))
    assert declared_tables == set(Base.metadata.tables)
    mermaid = (ROOT / "docs/ERD.md").read_text()
    diagram_entities = set(re.findall(r"^    ([A-Z_]+) \{$", mermaid, re.MULTILINE))
    assert diagram_entities == {table.upper() for table in Base.metadata.tables}
    for table in ("complaints", "complaint_events", "audit_logs"):
        assert f"CREATE TABLE {table} (" in schema
        assert re.search(r"\) PARTITION BY RANGE \(\w+\);", schema)
    for table in (
        "complaints",
        "complaint_keys",
        "complaint_events",
        "complaint_messages",
        "attachments",
        "notifications",
        "call_logs",
        "audit_logs",
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
        "export_job_rows",
    ):
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in rls


def test_live_openapi_is_valid_and_cannot_claim_unimplemented_routes() -> None:
    spec = yaml.safe_load((ROOT / "docs/openapi.yaml").read_text())
    validate(spec)
    assert spec == app.openapi()  # Snapshot drift fails CI, not just missing paths.
    assert len(spec["paths"]) >= 30
    assert spec["paths"]["/api/health/live"]["get"]["security"] == []
    assert spec["paths"]["/api/auth/refresh"]["post"]["security"] == [{"RefreshCookie": []}]
    reveal = spec["paths"]["/api/staff/complaints/{complaint_id}/national-id:reveal"]["post"]
    assert reveal["x-required-permission"] == "complaints.view_full_national_id"
    assert any(p["name"] == "X-CSRF-Token" for p in reveal["parameters"])
    assert spec["x-websockets"][0]["path"] == "/api/staff/complaints/stream"


def test_future_roadmap_retains_original_draft_without_claiming_routes_live() -> None:
    roadmap = yaml.safe_load((ROOT / "docs/openapi-roadmap.yaml").read_text())
    validate(roadmap)
    assert "/api/v1/admin/users/{userId}/access/preview" in roadmap["paths"]
    assert "/api/v1/customer/complaints/{ticketNo}/reopen" in roadmap["paths"]
    assert "ONLY /api/health/live" in roadmap["info"]["description"]


def test_orm_enum_labels_match_native_postgresql_enums() -> None:
    sql = (ROOT / "backend/alembic/sql/001_schema.sql").read_text()
    enum_map = {
        "insurance_type": INSURANCE_ENUM,
        "complaint_status": STATUS_ENUM,
        "complaint_priority": PRIORITY_ENUM,
        "staff_status": STAFF_ENUM,
        "permission_effect": EFFECT_ENUM,
        "ownership_level": OWNERSHIP_ENUM,
        "message_visibility": VISIBILITY_ENUM,
        "communication_channel": CHANNEL_ENUM,
        "message_direction": DIRECTION_ENUM,
        "attachment_scan_status": SCAN_ENUM,
    }
    for db_name, mapping in enum_map.items():
        raw = re.search(rf"CREATE TYPE {db_name} AS ENUM \(([^)]+)\);", sql)
        assert raw is not None
        assert mapping.enums == re.findall(r"'([^']+)'", raw.group(1))


def test_partition_month_math_and_synthetic_id_validation() -> None:
    assert add_months(date(2026, 12, 1), 2) == date(2027, 2, 1)
    assert add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)
    rng = random.Random(1)
    assert all(len(validate_national_id(synthetic_national_id(rng))) == 14 for _ in range(100))


def test_runtime_engine_rejects_admin_connection() -> None:
    with pytest.raises(ValueError, match="asyncpg"):
        make_engine("postgresql+psycopg://postgres@localhost/insurance")
    with pytest.raises(ValueError, match="app_runtime"):
        make_engine("postgresql+asyncpg://postgres@localhost/insurance")


def test_dev_compose_is_transaction_pooled_without_admin_secrets_in_api() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    assert {"postgres", "pgbouncer", "redis", "minio", "api", "worker", "nginx"} <= set(compose)
    assert compose["pgbouncer"]["environment"]["POOL_MODE"] == "transaction"
    assert compose["pgbouncer"]["environment"]["MAX_PREPARED_STATEMENTS"] == "100"
    for process in ("api", "worker"):
        assert "env_file" not in compose[process]
        assert "DATABASE_ADMIN_URL" not in compose[process]["environment"]
        assert "POSTGRES_PASSWORD" not in compose[process]["environment"]
        assert "MINIO_ROOT_PASSWORD" not in compose[process]["environment"]
