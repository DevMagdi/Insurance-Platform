"""Phase 3 role/identity guards and safe audit/notification contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from app.api import audit as audit_api
from app.api import auth as auth_api
from app.api import staff as staff_api
from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.domain.schemas import RoleSpec
from app.main import app
from app.repositories.complaints import ComplaintRepository
from app.services.auth import AuthService


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch):
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
    context = RequestContext(
        SimpleNamespace(), actor, uuid4(), 1, scope, frozenset(), {}, SimpleNamespace()
    )
    role_id = uuid4()
    repo = SimpleNamespace(
        roles=AsyncMock(return_value=[{"id": role_id, "name": "review", "permissions": []}]),
        save_role=AsyncMock(return_value=role_id),
        clone_role=AsyncMock(return_value=role_id),
        explain=AsyncMock(return_value={"editable": False, "access": {}, "why": {}}),
        edit_profile=AsyncMock(return_value=True),
        reactivate=AsyncMock(return_value=True),
        force_logout=AsyncMock(return_value=True),
    )
    audit_repo = SimpleNamespace(
        events=AsyncMock(
            return_value=[
                {
                    "id": uuid4(),
                    "created_at": datetime.now(UTC),
                    "actor_staff_id": actor.id,
                    "action": "=SUM(1)",
                    "resource_type": "role",
                    "resource_id": uuid4(),
                    "complaint_id": None,
                }
            ]
        ),
        access_changes=AsyncMock(return_value=[{"change_type": "access_replaced"}]),
    )
    auth = SimpleNamespace(
        limits=SimpleNamespace(check=AsyncMock()),
        reset_staff_password=AsyncMock(),
    )
    app.state.auth = auth
    monkeypatch.setattr(staff_api, "AccessRepository", lambda: repo)
    monkeypatch.setattr(audit_api, "AuditRepository", lambda: audit_repo)

    async def override():
        yield context

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"csrf_token": "value"},
        ) as client:
            yield client, context, repo, audit_repo, auth
    finally:
        app.dependency_overrides.clear()


async def test_admin_role_guards_and_password_reset_csrf(api: tuple) -> None:
    client, ctx, repo, _, auth = api
    headers = {"X-CSRF-Token": "value"}
    payload = {"name": "reviewer", "permissions": ["complaints.view"]}
    assert (await client.get("/api/staff/roles")).status_code == 403
    ctx.permissions = frozenset({"roles.manage", "users.view", "users.edit", "users.manage_access"})
    assert (await client.post("/api/staff/roles", json=payload)).status_code == 403
    assert (
        await client.post(
            "/api/staff/roles",
            json={
                "name": "reviewer",
                "permissions": ["unknown.admin"],
            },
            headers=headers,
        )
    ).status_code == 422
    result = await client.post("/api/staff/roles", json=payload, headers=headers)
    assert result.status_code == 201 and result.json()["id"] == str(repo.save_role.return_value)
    assert repo.save_role.call_args.args[4] == {"complaints.view"}
    user = uuid4()
    assert (await client.get(f"/api/staff/users/{user}/access/explain")).json()["why"] == {}
    assert (
        await client.post(f"/api/staff/users/{ctx.actor.id}/force-logout", headers=headers)
    ).status_code == 403
    assert (await client.post(f"/api/staff/users/{user}/password-reset")).status_code == 403
    assert (
        await client.post(f"/api/staff/users/{user}/password-reset", headers=headers)
    ).status_code == 202
    auth.reset_staff_password.assert_awaited_once_with(ctx.db, user)
    assert (
        await client.patch(
            f"/api/staff/users/{ctx.actor.id}",
            json={
                "email": "new@test.invalid",
                "full_name": "Changed Me",
            },
            headers=headers,
        )
    ).status_code == 403
    assert (
        await client.post(f"/api/staff/users/{user}/reactivate", headers=headers)
    ).status_code == 200
    assert (
        await client.post(f"/api/staff/users/{user}/force-logout", headers=headers)
    ).status_code == 200


async def test_access_change_audit_requires_both_permissions_and_global_scope(
    api: tuple,
) -> None:
    client, ctx, _, audit, auth = api
    url = "/api/staff/audit/access-changes"
    ctx.permissions = frozenset({"audit.view"})
    assert (await client.get(url)).status_code == 403
    ctx.permissions = frozenset({"audit.view", "users.manage_access"})
    ctx.scope = AccessScope(
        ctx.actor, company_ids=frozenset({uuid4()}), ownership=OwnershipLevel.OWN
    )
    assert (await client.get(url)).status_code == 403
    ctx.scope = AccessScope(ctx.actor, all_companies=True, ownership=OwnershipLevel.ALL)
    assert (await client.get(url)).json() == [{"change_type": "access_replaced"}]
    assert audit.access_changes.await_count == 1
    assert (
        await client.get(
            "/api/staff/audit/events?start=2026-01-01T00:00:00&end=2026-01-02T00:00:00Z"
        )
    ).status_code == 422
    assert (await client.get("/api/staff/audit/events?limit=501")).status_code == 422
    result = await client.get("/api/staff/audit/events.csv")
    assert result.status_code == 200 and "'=SUM(1)" in result.text
    assert "no-store" in result.headers["cache-control"]
    auth.limits.check.assert_awaited_once()


async def test_audit_filters_and_access_change_export_are_scoped_and_bounded(api: tuple) -> None:
    client, ctx, _, audit, auth = api
    ctx.permissions = frozenset({"audit.view", "users.manage_access"})
    ctx.scope = AccessScope(ctx.actor, all_companies=True, ownership=OwnershipLevel.ALL)
    actor_id, complaint_id, target_id = uuid4(), uuid4(), uuid4()
    event_response = await client.get(
        f"/api/staff/audit/events?actor_id={actor_id}&complaint_id={complaint_id}&action=status_changed"
    )
    assert event_response.status_code == 200
    assert audit.events.await_args.kwargs == {
        "actor_id": actor_id,
        "complaint_id": complaint_id,
        "action": "status_changed",
    }
    change = {
        "id": uuid4(),
        "created_at": datetime.now(UTC),
        "actor_staff_id": ctx.actor.id,
        "target_user_id": target_id,
        "change_type": "access_replaced",
        "before_state": {"roles": ["safe"]},
        "after_state": {"roles": ["updated"]},
    }
    audit.access_changes.return_value = [change]
    start, end = "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z"
    changes = await client.get(
        f"/api/staff/audit/access-changes?target_id={target_id}&start={start}&end={end}"
    )
    assert changes.status_code == 200
    assert audit.access_changes.await_args.args[1] == target_id
    assert audit.access_changes.await_args.args[4] is not None
    exported = await client.get(
        f"/api/staff/audit/access-changes.csv?target_id={target_id}&start={start}&end={end}"
    )
    assert exported.status_code == 200
    assert "access_replaced" in exported.text
    assert "no-store" in exported.headers["cache-control"]
    assert auth.limits.check.await_count == 1


async def test_staff_notifications_include_null_complaint_without_scope_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, company_ids=frozenset({uuid4()}), ownership=OwnershipLevel.OWN)
    notification = SimpleNamespace(
        id=uuid4(),
        kind="access_changed",
        payload={},
        created_at=datetime.now(UTC),
        read_at=None,
    )
    records = SimpleNamespace(all=lambda: [notification])
    db = SimpleNamespace(scalars=AsyncMock(return_value=records))
    result = await ComplaintRepository().notifications(db, scope)
    assert result == [notification]
    sql = str(db.scalars.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "LEFT OUTER JOIN complaints" in sql
    assert "notifications.complaint_id IS NULL OR" in sql
    assert "complaints.assigned_agent_id" in sql
    ctx = RequestContext(db, actor, uuid4(), 1, scope, frozenset(), {}, SimpleNamespace())
    monkeypatch.setattr(auth_api, "ComplaintRepository", lambda: ComplaintRepository())

    async def override():
        yield ctx

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            response = await client.get("/api/me/notifications")
            assert response.status_code == 200
            assert response.json()[0]["kind"] == "access_changed"
    finally:
        app.dependency_overrides.clear()


async def test_password_reset_delivery_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories import access as access_module

    repo = SimpleNamespace(issue_reset=AsyncMock(return_value="target@test.invalid"))
    monkeypatch.setattr(access_module, "AccessRepository", lambda: repo)
    sender = SimpleNamespace(send_password_reset=AsyncMock(side_effect=RuntimeError("offline")))
    service = AuthService.__new__(AuthService)
    service.settings = SimpleNamespace(public_app_url="https://public.example")
    service.delivery = sender
    with pytest.raises(HTTPException) as exc:
        await service.reset_staff_password(SimpleNamespace(), uuid4())
    assert exc.value.status_code == 503
    assert "token=" in sender.send_password_reset.call_args.args[1]
    assert repo.issue_reset.call_args.args[3] > datetime.now(UTC) + timedelta(hours=47)
    assert "target@test.invalid" in sender.send_password_reset.call_args.args[0]


def test_role_spec_rejects_unknown_permission() -> None:
    assert RoleSpec.model_validate({"name": "viewer", "permissions": []}).permissions == set()
    with pytest.raises(ValueError):
        RoleSpec.model_validate({"name": "viewer", "permissions": ["fictional.grant"]})
