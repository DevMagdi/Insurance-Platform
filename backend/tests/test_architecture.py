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


async def test_readiness_checks_postgres_and_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, _statement: object) -> None:
            return None

    class Engine:
        def connect(self) -> Connection:
            return Connection()

    class Redis:
        async def ping(self) -> bool:
            return True

    monkeypatch.setattr(app.state, "draining", False, raising=False)
    monkeypatch.setattr(app.state, "engine", Engine(), raising=False)
    monkeypatch.setattr(app.state, "redis", Redis(), raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/health/ready")).json() == {"status": "ready"}

        class UnavailableRedis:
            async def ping(self) -> bool:
                raise ConnectionError("Redis is down")

        monkeypatch.setattr(app.state, "redis", UnavailableRedis())
        response = await client.get("/api/health/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "Dependencies unavailable"}
        assert (await client.get("/api/health/live")).status_code == 200

        class UnavailableEngine:
            def connect(self) -> Connection:
                raise ConnectionError("PostgreSQL is down")

        monkeypatch.setattr(app.state, "engine", UnavailableEngine())
        monkeypatch.setattr(app.state, "redis", Redis())
        assert (await client.get("/api/health/ready")).status_code == 503


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
        "integrations.send_sms",
        "integrations.ai_call",
        "reports.manage",
        "audit.view",
        "settings.manage",
        "storage.manage",
    }
    assert required <= PERMISSIONS
    assert permission_parts("complaints.view") == ("complaints", "view")
    with pytest.raises(ValueError):
        permission_parts("not.a.permission")
    sql = (ROOT / "backend/alembic/sql/003_permissions.sql").read_text()
    seeded_permissions = set(re.findall(r"\('([^']+)',", sql))
    phase7 = (ROOT / "backend/alembic/sql/020_staff_console.sql").read_text()
    phase9 = (ROOT / "backend/alembic/sql/023_storage_console.sql").read_text()
    phase10 = (ROOT / "backend/alembic/sql/028_integration_connections.sql").read_text()
    assert (
        seeded_permissions
        | {
            "teams.manage",
            "storage.manage",
            "integrations.send_sms",
        }
        == PERMISSIONS
    )
    assert "VALUES ('teams.manage', 'teams', 'manage'" in phase7
    assert "VALUES ('storage.manage', 'storage', 'manage'" in phase9
    assert "VALUES ('integrations.send_sms', 'integrations', 'send_sms'" in phase10
    assert "WHERE name = 'administrator' AND is_system" in phase9
    assert "WHERE name = 'administrator' AND is_system" in phase10


def test_phase7_keeps_the_phase3_escalation_permission_contract() -> None:
    phase7 = (ROOT / "backend/alembic/sql/020_staff_console.sql").read_text()
    phase3 = (ROOT / "backend/alembic/sql/007_staff_workflow.sql").read_text()
    assert "CREATE FUNCTION app_security.staff_escalate(" not in phase7
    assert "CREATE OR REPLACE FUNCTION app_security.staff_escalate(" not in phase7
    assert "NOT app_security.has_permission('complaints.escalate')" in phase3
    assert "has_permission('integrations.send_email')" not in phase3


def test_sql_migrations_cover_every_requested_table_and_fail_closed_rls() -> None:
    schema = (ROOT / "backend/alembic/sql/001_schema.sql").read_text()
    rls = (ROOT / "backend/alembic/sql/002_access_rls.sql").read_text()
    for late in (
        "011_async_exports.sql",
        "013_scheduled_reports.sql",
        "015_analytics_rollup.sql",
        "021_privacy_operations.sql",
    ):
        rls += (ROOT / "backend/alembic/sql" / late).read_text()
    assert len(sqlparse.split(schema)) >= 70  # PL/pgSQL bodies must not be split
    assert len(sqlparse.split(rls)) >= 60
    # Phase 3 revisions are SQL-first too and must stay forward-only in Alembic.
    for revision, marker in (
        ("012_sla_calendars.sql", "app_security.simulate_sla"),
        ("013_scheduled_reports.sql", "app_security.claim_due_reports"),
        ("014_ops_admin.sql", "app_security.scan_duplicate_candidates"),
        ("015_analytics_rollup.sql", "app_security.analytics_daily_scoped"),
        ("016_export_formats.sql", "app_security.request_export"),
    ):
        body = (ROOT / "backend/alembic/sql" / revision).read_text()
        assert marker in body, revision
        assert "SECURITY DEFINER" in body
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
        for revision in (
            "004_core_tables.sql",
            "007_staff_workflow.sql",
            "011_async_exports.sql",
            "012_sla_calendars.sql",
            "013_scheduled_reports.sql",
            "014_ops_admin.sql",
            "015_analytics_rollup.sql",
            "016_export_formats.sql",
            "017_integrations_ai.sql",
            "021_privacy_operations.sql",
            "029_staff_onboarding.sql",
        )
    )
    declared_tables = set(re.findall(r"CREATE TABLE (?:public\.)?(\w+) \(", schema + additive))
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
        "report_runs",
        "analytics_rollup_state",
    ):
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in rls
    for table in ("consent_records", "privacy_requests"):
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in rls


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


def test_production_compose_pulls_published_images_without_admin_secrets() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())["services"]
    expected = {"postgres", "pgbouncer", "redis", "migrate", "api", "worker", "web"}
    # No object store is part of the platform: binaries live in PostgreSQL.
    assert not any("object-store" in name or "minio" in name for name in compose)
    assert expected <= set(compose)
    # Production runs the images the CD pipeline verified; it never builds on the host.
    for name in ("migrate", "api", "worker", "web"):
        image = compose[name]["image"]
        assert image.startswith("ghcr.io/${IMAGE_OWNER")
        assert "${IMAGE_TAG" in image
    assert not any("build" in service for service in compose.values())
    # The API and worker never receive the database administrator credentials.
    for process in ("api", "worker"):
        service = compose[process]
        assert "env_file" not in service
        for banned in (
            "DATABASE_ADMIN_URL",
            "POSTGRES_PASSWORD",
            "POSTGRES_USER",
        ):
            assert banned not in service["environment"], (process, banned)
        assert service["restart"] == "unless-stopped"
    assert "app_runtime" in compose["api"]["environment"]["DATABASE_URL"]
    assert "pgbouncer:5432" in compose["api"]["environment"]["DATABASE_URL"]
    assert "app_worker" in compose["worker"]["environment"]["WORKER_DATABASE_URL"]
    assert compose["api"]["environment"]["ENVIRONMENT"] == "production"
    volumes = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())["volumes"]
    # PostgreSQL, Redis and nothing else: no object-store volume to migrate or lose.
    assert set(volumes) == {"postgres_data", "redis_data"}
    assert compose["api"]["environment"]["COOKIE_SECURE"] == "true"
    # Migrations run first, as a one-shot container with the migrator URL only.
    migrate = compose["migrate"]
    assert "DATABASE_ADMIN_URL" in migrate["environment"]
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert migrate["restart"] == "no"
    for process in ("api", "worker"):
        depends = compose[process]["depends_on"]
        assert depends["migrate"] == {"condition": "service_completed_successfully"}
    assert compose["pgbouncer"]["environment"]["POOL_MODE"] == "transaction"
    assert compose["pgbouncer"]["environment"]["MAX_PREPARED_STATEMENTS"] == "100"
    # Exactly one publicly published port (the web edge); everything else is loopback.
    published = {
        name: service.get("ports", []) for name, service in compose.items() if service.get("ports")
    }
    assert set(published) == {"web", "postgres"}
    assert published["web"] == ["${HTTP_PORT:-8080}:80"]
    assert all(entry.startswith("127.0.0.1:") for entry in published["postgres"])
    # Every variable the stack demands must be documented outside the compose file.
    documented = (ROOT / ".env.example").read_text() + (ROOT / "docs/DEPLOYMENT.md").read_text()
    raw = (ROOT / "docker-compose.prod.yml").read_text()
    for name in set(re.findall(r"\$\{([A-Z_]+)(?::\?|:-)", raw)):
        assert name in documented, name


def test_cd_pipeline_gates_deployment_and_verifies_artifacts() -> None:
    cd = yaml.safe_load((ROOT / ".github/workflows/cd.yml").read_text())
    triggers = cd[True]
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["tags"] == ["v*"]
    assert "workflow_dispatch" in triggers
    jobs = cd["jobs"]
    assert {"images", "verify", "compose", "deploy", "release", "summary"} <= set(jobs)
    assert jobs["images"]["permissions"]["packages"] == "write"
    assert jobs["images"]["permissions"]["contents"] == "read"
    assert "if" not in jobs["images"]  # publishing is unconditional on main/tags
    # The deploy job is explicitly gated, runs in a protected environment and
    # never auto-merges or deploys without the repository variable being set.
    deploy = jobs["deploy"]
    assert "DEPLOY_ENABLED" in deploy["if"] and "inputs.deploy" in deploy["if"]
    assert deploy["environment"].startswith("${{")
    assert deploy["needs"] == ["images", "verify", "compose"]
    steps = yaml.dump(deploy)
    assert "--auto" not in steps
    assert steps.index("run --rm migrate") < steps.index("up -d --remove-orphans")
    assert "/api/health/ready" in steps
    assert "DEPLOY_SSH_KNOWN_HOSTS" in steps
    # Releasing is tag-only and must not invent versions.
    assert jobs["release"]["if"] == "github.ref_type == 'tag'"
    assert jobs["release"]["permissions"]["contents"] == "write"
    assert cd["concurrency"]["cancel-in-progress"] is False
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    containers = yaml.dump(ci["jobs"]["containers"])
    assert "Dockerfile.backend" in containers and "Dockerfile.web" in containers
    assert "docker-compose.prod.yml" in containers
    assert "api/health/live" in containers
    assert "openapi.yaml" in containers


def test_dev_compose_is_transaction_pooled_without_admin_secrets_in_api() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    assert {"postgres", "pgbouncer", "redis", "api", "worker", "nginx"} <= set(compose)
    assert not any("object-store" in name for name in compose)
    for process in ("api", "worker"):
        environment = compose[process]["environment"]
        assert not any(key.startswith("S3_") or key.startswith("MINIO_") for key in environment)
    assert compose["pgbouncer"]["environment"]["POOL_MODE"] == "transaction"
    assert compose["pgbouncer"]["environment"]["MAX_PREPARED_STATEMENTS"] == "100"
    for process in ("api", "worker"):
        assert "env_file" not in compose[process]
        assert "DATABASE_ADMIN_URL" not in compose[process]["environment"]
        assert "POSTGRES_PASSWORD" not in compose[process]["environment"]


def test_dev_stack_waits_for_backend_readiness_and_re_resolves_restarted_services() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    production = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())["services"]
    api_check = " ".join(compose["api"]["healthcheck"]["test"])
    prod_api_check = " ".join(production["api"]["healthcheck"]["test"])
    assert "/api/health/ready" in api_check
    assert "/api/health/ready" in prod_api_check
    assert compose["api"]["environment"]["WEB_CONCURRENCY"] == "${WEB_CONCURRENCY:-2}"
    assert compose["api"]["restart"] == "unless-stopped"
    assert compose["nginx"]["restart"] == "unless-stopped"
    assert "--wait" in (ROOT / "Makefile").read_text().split("up:\n", 1)[1].split("\n", 1)[0]

    nginx = (ROOT / "infra/nginx/dev.conf").read_text()
    assert "resolver 127.0.0.11 valid=5s ipv6=off;" in nginx
    assert "set $api_upstream api:8000;" in nginx
    assert "set $frontend_upstream frontend:5173;" in nginx
    assert "proxy_pass http://$api_upstream;" in nginx
    assert "proxy_pass http://$frontend_upstream;" in nginx
    assert "proxy_pass http://api:8000;" not in nginx
    assert "proxy_pass http://frontend:5173;" not in nginx

    production_nginx = (ROOT / "infra/nginx/prod.conf").read_text()
    for endpoint in ("/docs", "/redoc", "/openapi.json"):
        assert f"location = {endpoint} {{\n        return 404;\n    }}" in production_nginx
    assert "location ^~ /docs/ {\n        return 404;" in production_nginx
    assert "location ^~ /redoc/ {\n        return 404;" in production_nginx


# Object-storage SDKs, gateways and credentials were removed in revision 24. The
# guard below fails the build if any of them is reintroduced anywhere that runs
# in production (application code or deployment configuration). Documentation is
# excluded because it describes the removal on purpose, and the optional backup
# script upload is an operator-owned backup destination, not application storage.
_OBJECT_STORAGE_MARKERS = (
    "boto3",
    "botocore",
    "minio",
    "seaweed",
    "list_objects_v2",
    "listobjectsv2",
    "s3_bucket",
    "s3_endpoint",
    "s3_access_key",
    "put_object",
    "get_object",
    "presigned",
)


def test_no_object_storage_dependency_can_creep_back_in() -> None:
    scanned = [*sorted((ROOT / "backend/app").rglob("*.py"))]
    scanned += [
        ROOT / "backend/pyproject.toml",
        ROOT / "docker-compose.yml",
        ROOT / "docker-compose.prod.yml",
        ROOT / ".env.example",
        ROOT / "Makefile",
        ROOT / "deploy/helm/insurance-platform/values.yaml",
        ROOT / "deploy/helm/insurance-platform/templates/_helpers.tpl",
        ROOT / "deploy/helm/insurance-platform/templates/configmap.yaml",
        ROOT / "deploy/helm/insurance-platform/templates/worker.yaml",
        ROOT / "deploy/helm/insurance-platform/templates/api.yaml",
        ROOT / ".github/workflows/ci.yml",
        ROOT / ".github/workflows/cd.yml",
        ROOT / "infra/Dockerfile.backend",
        ROOT / "infra/Dockerfile.web",
    ]
    offenders: list[str] = []
    for path in scanned:
        text = path.read_text(encoding="utf-8").lower()
        for marker in _OBJECT_STORAGE_MARKERS:
            if marker in text:
                offenders.append(f"{path.relative_to(ROOT)}:{marker}")
    assert offenders == [], f"object storage is not part of this platform: {offenders}"


def test_private_binaries_are_columns_on_their_own_rows() -> None:
    sql = (ROOT / "backend/alembic/sql/024_database_storage.sql").read_text(encoding="utf-8")
    for table in ("attachments", "export_jobs", "call_logs"):
        assert f"ALTER TABLE public.{table}" in sql, table
    # Envelope overhead (2 magic + 1 key id + 12 nonce + 16 tag) is the floor of
    # every size CHECK and of every definer function that writes a blob.
    assert sql.count("BETWEEN 31 AND") >= 3
    # Both legacy locators are relaxed so the reservation path never writes a key.
    assert "ALTER TABLE public.attachment_uploads ALTER COLUMN storage_key DROP NOT NULL" in sql
    models = (ROOT / "backend/app/models/tickets.py").read_text(encoding="utf-8")
    assert "content: Mapped[bytes | None]" in models
    assert "recording_content: Mapped[bytes | None]" in models
    routers = (ROOT / "backend/app/api/storage_admin.py").read_text(encoding="utf-8")
    assert "storage_inventory" in routers and "bucket" not in routers.lower()
