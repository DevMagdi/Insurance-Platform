"""Phase 7 admin adapters preserve main's database-backed API contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import operations
from app.api import reports as reports_api
from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.domain.schemas import AssignmentRuleSpec, IntegrationSpec, ReportScheduleSpec, TeamSpec
from app.main import app
from app.repositories.admin import AdminRepository
from app.services.reports import ReportService


async def test_admin_repository_calls_migration_020_security_functions() -> None:
    team_id = uuid4()
    db = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                '{"companies": [], "teams": [], "roles": []}',
                '[{"id": "team"}]',
                team_id,
                '{"companies": [], "categories": [], "teams": [], "agents": []}',
            ]
        )
    )
    repository = AdminRepository()

    assert await repository.access_options(db) == {"companies": [], "teams": [], "roles": []}
    assert await repository.teams(db) == [{"id": "team"}]
    assert await repository.save_team(db, None, TeamSpec(name="Claims")) == team_id
    assert await repository.assignment_options(db) == {
        "companies": [],
        "categories": [],
        "teams": [],
        "agents": [],
    }
    statements = [str(call.args[0]) for call in db.scalar.await_args_list]
    assert statements == [
        "SELECT app_security.access_admin_options()",
        "SELECT app_security.admin_teams()",
        "SELECT app_security.save_team(:id, CAST(:data AS jsonb))",
        "SELECT app_security.admin_assignment_options()",
    ]
    assert json.loads(db.scalar.await_args_list[2].args[1]["data"])["name"] == "Claims"


@pytest.fixture
async def admin_client(monkeypatch: pytest.MonkeyPatch):
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
    context = RequestContext(
        SimpleNamespace(),
        actor,
        uuid4(),
        4,
        scope,
        frozenset({"users.manage_access", "teams.manage", "settings.manage"}),
        {"id": str(actor.id)},
        SimpleNamespace(),
    )
    team_id = uuid4()
    repository = SimpleNamespace(
        access_options=AsyncMock(return_value={"companies": [], "teams": [], "roles": []}),
        teams=AsyncMock(return_value=[]),
        save_team=AsyncMock(return_value=team_id),
        assignment_options=AsyncMock(
            return_value={"companies": [], "categories": [], "teams": [], "agents": []}
        ),
    )
    monkeypatch.setattr(operations, "AdminRepository", lambda: repository)

    async def override():
        yield context

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"csrf_token": "phase7-token"},
        ) as client:
            yield client, context, repository
    finally:
        app.dependency_overrides.clear()


async def test_global_team_and_automation_admin_routes_require_scope_and_csrf(
    admin_client: tuple,
) -> None:
    client, context, repository = admin_client
    assert (await client.get("/api/staff/admin/access-options")).status_code == 200
    assert (await client.get("/api/staff/admin/teams")).json() == []
    assert (await client.get("/api/staff/admin/automation-options")).status_code == 200

    payload = {"name": "Customer care", "member_ids": []}
    assert (await client.post("/api/staff/admin/teams", json=payload)).status_code == 403
    created = await client.post(
        "/api/staff/admin/teams",
        json=payload,
        headers={"X-CSRF-Token": "phase7-token"},
    )
    assert created.status_code == 201
    assert created.json() == {"id": str(repository.save_team.return_value)}
    assert repository.save_team.await_args.args[1] is None

    context.scope = AccessScope(
        context.actor, company_ids=frozenset({uuid4()}), ownership=OwnershipLevel.OWN
    )
    assert (await client.get("/api/staff/admin/access-options")).status_code == 403
    assert (await client.get("/api/staff/admin/teams")).status_code == 403
    assert (await client.get("/api/staff/admin/automation-options")).status_code == 403


class ReportCaptureRepository:
    def __init__(self) -> None:
        self.payload: dict[str, object] | None = None

    async def save(self, _db: object, _schedule_id: object, payload: str) -> object:
        self.payload = json.loads(payload)
        return uuid4()


async def test_report_service_computes_next_cron_occurrence_on_server() -> None:
    repository = ReportCaptureRepository()
    service = ReportService(repository)  # type: ignore[arg-type]
    client_supplied_time = datetime(2000, 1, 1, tzinfo=UTC)
    spec = ReportScheduleSpec(
        name="Weekly review",
        filters={"window_days": 30, "company_id": uuid4()},
        cron_expression="0 8 * * 1",
        time_zone="Africa/Cairo",
        recipient_emails=["ops@example.com"],
        next_run_at=client_supplied_time,
        is_active=True,
    )

    await service.save(SimpleNamespace(), None, spec)

    assert repository.payload is not None
    next_run = datetime.fromisoformat(str(repository.payload["next_run_at"]))
    assert next_run.tzinfo is not None and next_run > datetime.now(UTC)
    assert next_run != client_supplied_time
    assert repository.payload["filters"]["window_days"] == 30  # type: ignore[index]


async def test_report_routes_preserve_owner_scoped_contract_and_locked_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = Actor(uuid4(), "staff")
    context = RequestContext(
        SimpleNamespace(),
        actor,
        uuid4(),
        4,
        AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL),
        frozenset({"reports.manage", "analytics.view", "analytics.export"}),
        {"id": str(actor.id)},
        SimpleNamespace(),
    )
    schedule_id = uuid4()
    fake_service = SimpleNamespace(
        schedules=AsyncMock(return_value=[]),
        save=AsyncMock(return_value=schedule_id),
        delete=AsyncMock(return_value=True),
        queue_now=AsyncMock(return_value=True),
        runs=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(reports_api, "service", lambda: fake_service)

    async def override():
        yield context

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"csrf_token": "report-token"},
        ) as client:
            assert (await client.get("/api/staff/reports/schedules")).json() == []
            payload = {
                "name": "Weekly review",
                "filters": {"window_days": 14},
                "cron_expression": "0 8 * * 1",
                "time_zone": "Africa/Cairo",
                "recipient_emails": ["ops@example.com"],
                "format": "xlsx",
                "is_active": True,
            }
            assert (
                await client.post("/api/staff/reports/schedules", json=payload)
            ).status_code == 403
            created = await client.post(
                "/api/staff/reports/schedules",
                json=payload,
                headers={"X-CSRF-Token": "report-token"},
            )
            assert created.status_code == 201
            assert created.json() == {"id": str(schedule_id)}
            assert fake_service.save.await_args.args[2].filters.window_days == 14
            assert (
                await client.post(
                    f"/api/staff/reports/schedules/{schedule_id}/run",
                    headers={"X-CSRF-Token": "report-token"},
                )
            ).status_code == 202
            context.permissions = frozenset({"reports.manage", "analytics.view"})
            assert (
                await client.post(
                    f"/api/staff/reports/schedules/{schedule_id}/run",
                    headers={"X-CSRF-Token": "report-token"},
                )
            ).status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_phase7_schemas_reject_unsafe_or_absolute_scheduled_report_filters() -> None:
    lead_id = uuid4()
    team = TeamSpec(name="Claims", lead_staff_id=lead_id, member_ids=[lead_id])
    assert team.lead_staff_id == lead_id
    with pytest.raises(ValidationError):
        TeamSpec.model_validate({"name": "Claims", "lead_staff_id": lead_id, "member_ids": []})
    with pytest.raises(ValidationError):
        TeamSpec.model_validate({"name": "Claims", "member_ids": [lead_id, lead_id]})

    with pytest.raises(ValidationError):
        AssignmentRuleSpec.model_validate({"strategy": "least_workload"})
    with pytest.raises(ValidationError):
        IntegrationSpec.model_validate({"provider": "email", "is_enabled": True})
    with pytest.raises(ValidationError):
        IntegrationSpec.model_validate({"provider": "email", "secret_ref": "secret with spaces"})

    schedule = ReportScheduleSpec.model_validate(
        {
            "name": "Weekly report",
            "filters": {"window_days": 7},
            "cron_expression": "0 8 * * 1",
            "time_zone": "Africa/Cairo",
            "recipient_emails": ["ops@example.com"],
        }
    )
    assert schedule.next_run_at is None and schedule.is_active
    with pytest.raises(ValidationError):
        ReportScheduleSpec.model_validate(
            {
                "name": "Pinned report",
                "filters": {"date_from": "2026-10-01T00:00:00Z", "window_days": 7},
                "cron_expression": "0 8 * * 1",
                "time_zone": "Africa/Cairo",
                "recipient_emails": ["ops@example.com"],
            }
        )
    normalized = ReportScheduleSpec.model_validate(
        {
            "name": "Normalized recipient",
            "filters": {"window_days": 7},
            "cron_expression": "0 8 * * 1",
            "time_zone": "Africa/Cairo",
            "recipient_emails": ["Ops@Example.com"],
        }
    )
    assert normalized.recipient_emails == ["ops@example.com"]
