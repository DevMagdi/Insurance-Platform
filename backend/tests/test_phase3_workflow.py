"""Staff workflow guards, atomic bulk contracts and scoped query construction."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api import workflow
from app.api.deps import RequestContext, authenticated_context, require_permission
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import ComplaintStatus, OwnershipLevel
from app.domain.schemas import AssignmentRequest, BulkAction, ComplaintFilter, SavedViewCreate
from app.main import app
from app.repositories.complaints import ComplaintRepository
from app.services.workflow import StaffWorkflowService


@pytest.fixture
async def http(monkeypatch: pytest.MonkeyPatch):
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
    complaints = SimpleNamespace(
        parent=AsyncMock(
            return_value=SimpleNamespace(
                id=uuid4(), created_at=datetime.now(UTC), status=ComplaintStatus.NEW
            )
        ),
        list=AsyncMock(return_value={"items": [], "total": 0, "page": 1, "page_size": 20}),
        change_status=AsyncMock(return_value={"status": ComplaintStatus.RESOLVED}),
    )
    db = SimpleNamespace(flush=AsyncMock())
    context = RequestContext(db, actor, uuid4(), 0, scope, frozenset(), {}, complaints)
    repo = SimpleNamespace(
        assign=AsyncMock(return_value=True),
        escalate=AsyncMock(return_value=True),
        merge=AsyncMock(return_value=True),
        candidates=AsyncMock(return_value=[]),
        canned_replies=AsyncMock(return_value=[]),
        saved_views=AsyncMock(return_value=[]),
        saved_view=AsyncMock(return_value=SimpleNamespace(criteria={"search": "claim"})),
        create_view=AsyncMock(
            return_value=SimpleNamespace(id=uuid4(), name="My tickets", criteria={})
        ),
        update_view=AsyncMock(),
        delete_view=AsyncMock(return_value=True),
        mark_read=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(workflow, "StaffWorkflowRepository", lambda: repo)

    async def override():
        yield context

    app.dependency_overrides[authenticated_context] = override
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
        cookies={"csrf_token": "known"},
    ) as client:
        yield client, context, complaints, repo
    app.dependency_overrides.clear()


async def test_staff_workflow_permission_csrf_scope_and_bulk(http: tuple) -> None:
    client, context, complaints, repo = http
    target, agent, duplicate = uuid4(), uuid4(), uuid4()
    assign_path = f"/api/staff/complaints/{target}/assign"
    payload = {"agent_id": str(agent)}
    assert (
        await client.post(assign_path, json=payload, headers={"X-CSRF-Token": "known"})
    ).status_code == 403
    context.permissions = frozenset({"complaints.assign", "complaints.view"})
    assert (await client.post(assign_path, json=payload)).status_code == 403
    result = await client.post(assign_path, json=payload, headers={"X-CSRF-Token": "known"})
    assert result.status_code == 200 and result.json()["status"] == "assigned"
    assert repo.assign.call_args.args[0] == context.db
    assert complaints.parent.call_args.args[1] == context.scope
    denied = await client.post(
        f"/api/staff/complaints/{target}/reassign", json=payload, headers={"X-CSRF-Token": "known"}
    )
    assert denied.status_code == 403
    context.permissions = frozenset(
        {"complaints.view", "complaints.reassign", "complaints.escalate", "complaints.merge"}
    )
    assert (
        await client.post(
            f"/api/staff/complaints/{target}/reassign",
            json=payload,
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 200
    escalation_url = f"/api/staff/complaints/{target}/escalate"
    escalation_body = {"reason": "Contact insurer urgently"}
    headers = {"X-CSRF-Token": "known"}
    assert (
        await client.post(escalation_url, json=escalation_body, headers=headers)
    ).status_code == 403
    context.permissions = frozenset({*context.permissions, "integrations.send_email"})
    assert (await client.post(escalation_url, json=escalation_body, headers=headers)).json()[
        "delivery"
    ] == "queued"
    assert (
        await client.post(
            f"/api/staff/complaints/{target}/merge",
            json={"target_id": str(duplicate)},
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 200
    assert repo.merge.call_args.args[0] == context.db
    assert (await client.get(f"/api/staff/complaints/{target}/duplicates")).json() == []
    # Bulk status cannot piggyback on view or assign grants.
    bulk = "/api/staff/complaints/bulk"
    assert (
        await client.post(
            bulk,
            json={"action": "status", "complaint_ids": [str(target)], "status": "resolved"},
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 403
    context.permissions = frozenset({*context.permissions, "complaints.change_status"})
    done = await client.post(
        bulk,
        json={"action": "status", "complaint_ids": [str(target)], "status": "resolved"},
        headers={"X-CSRF-Token": "known"},
    )
    assert done.status_code == 200 and done.json()["count"] == 1
    assert complaints.change_status.call_args.args[1] == context.scope


async def test_workflow_guards_visibility_and_rolls_back_on_failed_bulk(http: tuple) -> None:
    _client, ctx, complaints, repo = http
    service = StaffWorkflowService(complaints, repo)
    first, second = uuid4(), uuid4()
    assignment = AssignmentRequest(agent_id=uuid4())
    repo.assign.side_effect = [True, False]
    with pytest.raises(HTTPException) as exc:
        await service.bulk(
            ctx.db,
            ctx.scope,
            BulkAction(action="assign", complaint_ids=[first, second], assignment=assignment),
        )
    assert exc.value.status_code == 409
    assert repo.assign.await_count == 2  # Transaction dependency rolls back both.
    repo.assign.side_effect = None
    repo.assign.return_value = True
    with pytest.raises(HTTPException) as exc:
        await service.merge(ctx.db, ctx.scope, first, first)
    assert exc.value.status_code == 422
    repo.saved_view.return_value = None
    with pytest.raises(HTTPException) as exc:
        await service.saved_view_results(ctx.db, ctx.scope, uuid4())
    assert exc.value.status_code == 404
    repo.saved_view.return_value = SimpleNamespace(criteria={"company_id": str(uuid4())})
    await service.saved_view_results(ctx.db, ctx.scope, uuid4())
    assert complaints.list.call_args.args[1] == ctx.scope
    actor = Actor(uuid4(), "customer")
    with pytest.raises(HTTPException):
        await service.assign(ctx.db, AccessScope.for_customer(actor), first, assignment)


async def test_private_views_and_notifications_are_owner_and_permission_gated(http: tuple) -> None:
    client, ctx, complaints, repo = http
    view_id = uuid4()
    assert (await client.get("/api/staff/views")).status_code == 403
    ctx.permissions = frozenset({"complaints.view"})
    assert (await client.get("/api/staff/views")).json() == []
    assert (
        await client.post("/api/staff/views", json={"name": "My tickets", "criteria": {"page": 2}})
    ).status_code == 403
    created = await client.post(
        "/api/staff/views",
        json={"name": "My tickets", "criteria": {"page": 2}},
        headers={"X-CSRF-Token": "known"},
    )
    assert created.status_code == 201
    assert repo.create_view.call_args.args[1] == ctx.scope
    result = await client.get(f"/api/staff/views/{view_id}/complaints")
    assert result.status_code == 200 and complaints.list.call_args.args[1] == ctx.scope
    assert (
        await client.patch(
            f"/api/staff/notifications/{uuid4()}/read", headers={"X-CSRF-Token": "known"}
        )
    ).status_code == 200
    assert repo.mark_read.call_args.args[1] == ctx.scope


def test_filter_sorts_and_optional_facts_keep_company_type_owner_predicates() -> None:
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, company_ids=frozenset({uuid4()}), ownership=OwnershipLevel.OWN)
    item = ComplaintFilter(
        status="new",
        date_from="2026-01-01T00:00:00+02:00",
        date_to="2026-02-01T00:00:00+02:00",
        company_id=next(iter(scope.company_ids)),
        priority="high",
        category_id=uuid4(),
        channel="email",
        sort_by="priority",
        sort_dir="asc",
        search="lost claim",
        assigned_agent_id=actor.id,
    )
    assert item.date_from == datetime(2025, 12, 31, 22, tzinfo=UTC)
    query = str(ComplaintRepository().base_query(scope, item).compile(dialect=postgresql.dialect()))
    for name in (
        "company_id",
        "assigned_agent_id",
        "priority",
        "category_id",
        "channel",
        "created_at",
        "search_vector",
        "status",
    ):
        assert f"complaints.{name}" in query
    with pytest.raises(ValidationError):
        ComplaintFilter(date_from="2026-01-02T00:00:00", date_to="2026-01-01T00:00:00Z")
    with pytest.raises(ValidationError):
        ComplaintFilter(sort_by="DROP TABLE complaints")
    for body in (
        {"action": "assign", "complaint_ids": [str(uuid4())]},
        {"action": "status", "complaint_ids": [str(uuid4())], "status": "transferred"},
    ):
        with pytest.raises(ValidationError):
            BulkAction.model_validate(body)
    assert SavedViewCreate(name="Mine", criteria=ComplaintFilter()).criteria.page == 1


def test_role_permission_scope_matrix_stays_deny_first() -> None:
    actor = Actor(uuid4(), "staff")
    company = uuid4()
    for granted, allowed_scope, expected in (
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (True, True, True),
    ):
        scope = AccessScope(
            actor,
            company_ids=frozenset({company}) if allowed_scope else frozenset(),
            ownership=OwnershipLevel.OWN,
        )
        ctx = RequestContext(
            SimpleNamespace(),
            actor,
            uuid4(),
            0,
            scope,
            frozenset({"complaints.view"}) if granted else frozenset(),
            None,
            SimpleNamespace(),
        )
        if not granted:
            with pytest.raises(HTTPException):
                require_permission("complaints.view").verify(ctx)
        else:
            require_permission("complaints.view").verify(ctx)
        predicate = str(scope.predicate())
        assert ("false" not in predicate.lower()) == allowed_scope
        assert (granted and allowed_scope) is expected
