"""Phase 7 admin endpoints retain global-scope, permission and provider-boundary guards."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import operations
from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.domain.schemas import (
    AssignmentRuleSpec,
    IntegrationConfigSpec,
    ReportScheduleSpec,
    TeamSpec,
)
from app.main import app
from app.repositories.admin import AdminRepository


@pytest.mark.asyncio
async def test_admin_repository_adapts_fixed_metadata_functions() -> None:
    owner_id, team_id, rule_id, schedule_id = (uuid4() for _ in range(4))
    scheduled_row = SimpleNamespace(_mapping={"id": schedule_id, "name": "Weekly"})
    db = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                '{"companies": [], "teams": [], "roles": []}',
                "[]",
                team_id,
                '{"companies": [], "teams": [], "agents": []}',
                "[]",
                rule_id,
                schedule_id,
                "[]",
                True,
            ]
        ),
        execute=AsyncMock(return_value=[scheduled_row]),
    )
    repository = AdminRepository()

    assert await repository.access_options(db) == {
        "companies": [],
        "teams": [],
        "roles": [],
    }
    assert await repository.teams(db) == []
    assert await repository.save_team(db, None, TeamSpec(name="Claims")) == team_id
    assert await repository.assignment_options(db) == {
        "companies": [],
        "teams": [],
        "agents": [],
    }
    assert await repository.assignment_rules(db) == []
    assert (
        await repository.save_assignment_rule(
            db,
            None,
            AssignmentRuleSpec(target_team_id=team_id),
        )
        == rule_id
    )
    assert await repository.report_schedules(db, owner_id) == [
        {"id": schedule_id, "name": "Weekly"}
    ]
    assert (
        await repository.save_report_schedule(
            db,
            owner_id,
            None,
            ReportScheduleSpec(name="Weekly", filters={}, recipient_emails=["ops@example.com"]),
        )
        == schedule_id
    )
    assert await repository.integrations(db) == []
    assert await repository.save_integration(db, "email", IntegrationConfigSpec())
    assert db.scalar.await_count == 9
    db.execute.assert_awaited_once()


@pytest.fixture
async def console(monkeypatch: pytest.MonkeyPatch):
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
    context = RequestContext(
        SimpleNamespace(),
        actor,
        uuid4(),
        4,
        scope,
        frozenset(
            {
                "users.manage_access",
                "teams.manage",
                "settings.manage",
                "integrations.manage",
                "reports.manage",
            }
        ),
        {"id": str(actor.id)},
        SimpleNamespace(),
    )
    team_id, rule_id = uuid4(), uuid4()
    repo = SimpleNamespace(
        access_options=AsyncMock(return_value={"companies": [], "teams": [], "roles": []}),
        teams=AsyncMock(return_value=[]),
        save_team=AsyncMock(return_value=team_id),
        assignment_options=AsyncMock(return_value={"companies": [], "teams": [], "agents": []}),
        assignment_rules=AsyncMock(return_value=[]),
        save_assignment_rule=AsyncMock(return_value=rule_id),
        report_schedules=AsyncMock(return_value=[]),
        save_report_schedule=AsyncMock(return_value=uuid4()),
        integrations=AsyncMock(return_value=[]),
        save_integration=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(operations, "AdminRepository", lambda: repo)

    async def override():
        yield context

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"csrf_token": "token"},
        ) as client:
            yield client, context, repo
    finally:
        app.dependency_overrides.clear()


async def test_admin_options_and_team_changes_require_permission_scope_and_csrf(
    console: tuple,
) -> None:
    client, context, repo = console
    assert (await client.get("/api/staff/admin/access-options")).status_code == 200
    assert (await client.get("/api/staff/admin/teams")).status_code == 200
    team = {"name": "Customer care", "member_ids": []}
    assert (await client.post("/api/staff/admin/teams", json=team)).status_code == 403
    created = await client.post(
        "/api/staff/admin/teams", json=team, headers={"X-CSRF-Token": "token"}
    )
    assert created.status_code == 201
    assert created.json()["id"] == str(repo.save_team.return_value)
    assert repo.save_team.await_args.args[1] is None
    context.scope = AccessScope(
        context.actor, company_ids=frozenset({uuid4()}), ownership=OwnershipLevel.OWN
    )
    assert (await client.get("/api/staff/admin/access-options")).status_code == 403
    assert (await client.get("/api/staff/admin/teams")).status_code == 403


async def test_automation_and_integration_admin_routes_are_permission_gated(console: tuple) -> None:
    client, context, repo = console
    assert (await client.get("/api/staff/admin/automation-options")).status_code == 200
    assert (await client.get("/api/staff/admin/assignment-rules")).status_code == 200
    rule = {
        "target_agent_id": str(uuid4()),
        "strategy": "least_workload",
        "sort_order": 10,
        "is_active": True,
    }
    assert (await client.post("/api/staff/admin/assignment-rules", json=rule)).status_code == 403
    saved_rule = await client.post(
        "/api/staff/admin/assignment-rules", json=rule, headers={"X-CSRF-Token": "token"}
    )
    assert saved_rule.status_code == 201
    assert saved_rule.json()["id"] == str(repo.save_assignment_rule.return_value)
    assert (await client.get("/api/staff/admin/integrations")).status_code == 200
    bad_ref = await client.put(
        "/api/staff/admin/integrations/email",
        json={"enabled": True, "secret_ref": "plain-password"},
        headers={"X-CSRF-Token": "token"},
    )
    assert bad_ref.status_code == 422
    response = await client.put(
        "/api/staff/admin/integrations/email",
        json={"enabled": True, "secret_ref": "vault://insurance/email"},
        headers={"X-CSRF-Token": "token"},
    )
    assert response.status_code == 200
    assert response.json() == {"saved": True, "runtime_ready": False}
    assert repo.save_integration.await_args.args[1] == "email"
    schedule = await client.get("/api/staff/admin/report-schedules")
    assert schedule.status_code == 200 and schedule.json()["delivery_ready"] is False
    schedule_payload = {
        "name": "Weekly review",
        "filters": {},
        "cron_expression": "0 8 * * 1",
        "time_zone": "Africa/Cairo",
        "recipient_emails": ["ops@example.invalid"],
        "is_active": False,
    }
    saved_schedule = await client.post(
        "/api/staff/admin/report-schedules",
        json=schedule_payload,
        headers={"X-CSRF-Token": "token"},
    )
    assert saved_schedule.status_code == 201 and saved_schedule.json()["delivery_ready"] is False
    assert repo.save_report_schedule.await_args.args[1] == context.actor.id
    context.permissions = frozenset({"users.manage_access"})
    assert (await client.get("/api/staff/admin/integrations")).status_code == 403


def test_phase7_admin_schemas_reject_unsafe_or_incomplete_values() -> None:
    lead = uuid4()
    assert (
        TeamSpec.model_validate(
            {"name": "Claims", "lead_staff_id": lead, "member_ids": [lead]}
        ).lead_staff_id
        == lead
    )
    with pytest.raises(ValidationError):
        TeamSpec.model_validate({"name": "Claims", "lead_staff_id": lead, "member_ids": []})
    with pytest.raises(ValidationError):
        AssignmentRuleSpec.model_validate({"strategy": "least_workload"})
    with pytest.raises(ValidationError):
        IntegrationConfigSpec.model_validate({"enabled": True, "secret_ref": "secret-password"})
    schedule = ReportScheduleSpec.model_validate(
        {
            "name": "Weekly report",
            "filters": {},
            "recipient_emails": ["ops@example.invalid"],
        }
    )
    assert schedule.is_active is False
    with pytest.raises(ValidationError):
        ReportScheduleSpec.model_validate(
            {"name": "Weekly report", "filters": {}, "recipient_emails": ["Ops@Example.invalid"]}
        )
