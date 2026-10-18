"""HTTP routes enforce permissions, CSRF, validation and no-scope disclosure."""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import customer, staff
from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.main import app
from app.security.tokens import TokenManager
from app.settings import Settings


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="test",
    )
    complaints = SimpleNamespace(
        list=AsyncMock(return_value={"items": [], "total": 0, "page": 1, "page_size": 20}),
        detail=AsyncMock(
            return_value={"ticket_no": "CMP-2026-000001", "national_id_masked": "**********2345"}
        ),
        parent=AsyncMock(return_value=SimpleNamespace(id=uuid4(), created_at=datetime.now(UTC))),
        export=AsyncMock(return_value=b"ticket_no\r\n"),
        reveal_national_id=AsyncMock(return_value="29901010112345"),
        reply=AsyncMock(return_value={"body": "Reply"}),
        create=AsyncMock(),
        summary=Mock(return_value={"ticket_no": "CMP-2026-000001"}),
        csat=AsyncMock(return_value={"submitted": True}),
        reopen=AsyncMock(return_value={"status": "reopened"}),
        change_status=AsyncMock(return_value={"status": "resolved"}),
    )
    auth = SimpleNamespace(
        settings=settings,
        tokens=TokenManager(settings),
        limits=SimpleNamespace(check=AsyncMock()),
        protector=SimpleNamespace(blind_index=Mock(return_value=b"b" * 32)),
        create_invitation=AsyncMock(return_value=uuid4()),
    )
    app.state.auth = auth
    app.state.factory = SimpleNamespace()
    app.state.complaints = complaints
    app.state.redis = SimpleNamespace(delete=AsyncMock())
    app.state.attachments = SimpleNamespace(
        plan=AsyncMock(return_value=[{"id": str(uuid4()), "upload_url": "/api/customer/upload"}]),
        clean_attachment=AsyncMock(),
        authorize_download=AsyncMock(return_value={"download_url": "/api/staff/download"}),
        upload=AsyncMock(return_value={"scan_status": "pending"}),
        download=AsyncMock(
            return_value=(
                b"data",
                SimpleNamespace(mime_type="application/pdf", original_filename="report.pdf"),
            )
        ),
    )
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())
    current = {
        "ctx": RequestContext(
            db, actor, uuid4(), 0, scope, frozenset(), {"id": str(actor.id)}, complaints
        )
    }

    async def override_context():
        yield current["ctx"]

    app.dependency_overrides[authenticated_context] = override_context
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
        cookies={"csrf_token": "known"},
    ) as client:
        yield client, current, complaints, auth
    app.dependency_overrides.clear()


async def test_staff_permission_required_on_list_filter_search_export_and_reveal(
    api: tuple,
) -> None:
    client, current, service, _auth = api
    for path in (
        "/api/staff/complaints",
        "/api/staff/complaints/counters",
        "/api/staff/complaints/export",
        f"/api/staff/complaints/{uuid4()}",
        "/api/staff/notifications",
        "/api/staff/users",
        f"/api/staff/attachments/{uuid4()}/authorize",
    ):
        result = await client.get(path)
        assert result.status_code == 403, path
    current["ctx"].permissions = frozenset({"complaints.view", "complaints.export"})
    result = await client.get("/api/staff/complaints?status=new&search=claim&page=2")
    assert result.status_code == 200, result.text
    assert service.list.call_args.args[1] == current["ctx"].scope
    assert service.list.call_args.args[2].search == "claim"
    result = await client.get("/api/staff/complaints/export?company_id=" + str(uuid4()))
    assert result.status_code == 200 and "attachment" in result.headers["content-disposition"]
    assert service.export.call_args.args[1] == current["ctx"].scope
    result = await client.get("/api/staff/complaints?unknown_filter=all")
    assert result.status_code == 422
    service.detail.side_effect = HTTPException(status_code=404, detail="Complaint not found")
    result = await client.get(f"/api/staff/complaints/{uuid4()}")
    assert result.status_code == 404
    assert "national_id" not in result.text
    current["ctx"].permissions = frozenset({"complaints.view_full_national_id"})
    target = f"/api/staff/complaints/{uuid4()}/national-id:reveal"
    assert (
        await client.post(target, json={"reason": "Case verification needed"})
    ).status_code == 403
    result = await client.post(
        target, json={"reason": "Case verification needed"}, headers={"X-CSRF-Token": "known"}
    )
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert service.reveal_national_id.await_count == 1


async def test_staff_admin_own_access_guard_and_guest_cannot_call_staff(api: tuple) -> None:
    client, current, _service, _auth = api
    current["ctx"].permissions = frozenset({"users.manage_access"})
    current_id = current["ctx"].actor.id
    result = await client.put(
        f"/api/staff/users/{current_id}/access",
        json={"all_companies": True, "ownership": "all"},
        headers={"X-CSRF-Token": "known"},
    )
    assert result.status_code == 403 and "own" in result.json()["detail"]
    customer_actor = Actor(uuid4(), "customer")
    current["ctx"].actor = customer_actor
    current["ctx"].scope = AccessScope.for_customer(customer_actor)
    assert (await client.get("/api/staff/complaints")).status_code == 403


async def test_customer_routes_validate_idempotency_csrf_and_id_guessing(api: tuple) -> None:
    client, current, service, _auth = api
    actor = Actor(uuid4(), "customer")
    current["ctx"].actor = actor
    current["ctx"].scope = AccessScope.for_customer(actor)
    record = SimpleNamespace(id=uuid4())
    service.create.return_value = (record, False)
    payload = {
        "national_id": "29901010112345",
        "mobile": "+201123456789",
        "company_id": str(uuid4()),
        "insurance_type": "health",
        "policy_number": "POL-123456",
        "subject": "Delayed claim payment",
        "description": "The claim remains unsettled after several weeks.",
    }
    url = "/api/customer/complaints"
    result = await client.post(url, json=payload, headers={"Idempotency-Key": "test-key-1234"})
    assert result.status_code == 403
    result = await client.post(
        url,
        json={**payload, "national_id": "bad"},
        headers={"Idempotency-Key": "test-key-1234", "X-CSRF-Token": "known"},
    )
    assert result.status_code == 422
    result = await client.post(
        url, json=payload, headers={"Idempotency-Key": "test-key-1234", "X-CSRF-Token": "known"}
    )
    assert result.status_code == 201 and result.json()["replayed"] is False
    assert service.create.call_args.args[1] == current["ctx"].scope
    service.create.return_value = (record, True)
    result = await client.post(
        url, json=payload, headers={"Idempotency-Key": "test-key-1234", "X-CSRF-Token": "known"}
    )
    assert result.status_code == 200 and result.json()["replayed"] is True
    service.detail.side_effect = HTTPException(status_code=404, detail="Complaint not found")
    assert (await client.get(f"{url}/{uuid4()}")).status_code == 404
    assert (await client.get("/api/staff/complaints")).status_code == 403


async def test_signed_attachment_routes_require_own_cookie_and_csrf(api: tuple) -> None:
    client, current, _service, _auth = api
    plan = {
        "files": [
            {
                "filename": "proof.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 100,
                "checksum_sha256": "a" * 64,
            }
        ]
    }
    url = f"/api/staff/complaints/{uuid4()}/attachments/plan"
    assert (await client.post(url, json=plan, headers={"X-CSRF-Token": "known"})).status_code == 403
    current["ctx"].permissions = frozenset({"complaints.view_attachments"})
    assert (await client.post(url, json=plan)).status_code == 403
    response = await client.post(url, json=plan, headers={"X-CSRF-Token": "known"})
    assert response.status_code == 200, response.text
    assert response.json()["uploads"][0]["upload_url"].startswith("/api/")
    result = await client.get(
        f"/api/staff/attachments/{uuid4()}/content", headers={"X-Download-Token": "token"}
    )
    assert result.status_code == 200 and result.headers["x-content-type-options"] == "nosniff"


async def test_guest_track_only_returns_minimal_status(
    api: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _current, _service, _auth = api

    @asynccontextmanager
    async def transaction(factory: object, purpose: str):
        assert purpose == "guest"
        yield SimpleNamespace()

    monkeypatch.setattr(customer, "purpose_transaction", transaction)
    repository = SimpleNamespace(
        guest_track=AsyncMock(
            return_value={
                "public_status": "new",
                "public_ticket": "CMP-2026-000001",
                "public_company": "Nile",
            }
        )
    )
    monkeypatch.setattr(customer, "ComplaintRepository", lambda: repository)
    result = await client.post(
        "/api/guest/track", json={"national_id": "29901010112345", "ticket_no": "CMP-2026-000001"}
    )
    assert result.status_code == 200
    assert "mobile" not in result.json() and "national_id" not in result.json()
    assert repository.guest_track.call_args.args[2] == b"b" * 32
    repository.guest_track.return_value = None
    assert (
        await client.post(
            "/api/guest/track",
            json={"national_id": "29901010112345", "ticket_no": "CMP-2026-000002"},
        )
    ).status_code == 404


async def test_staff_admin_access_and_2fa_routes_reevaluate_permission(
    api: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, current, _service, _auth = api
    repository = SimpleNamespace(
        staff_directory=AsyncMock(return_value=[{"email": "staff@example.test"}]),
        snapshot=AsyncMock(return_value={"id": str(uuid4()), "permissions": []}),
        replace=AsyncMock(return_value=4),
        deactivate=AsyncMock(return_value=True),
        require_totp=AsyncMock(return_value=5),
        require_global_totp=AsyncMock(),
    )
    monkeypatch.setattr(staff, "AccessRepository", lambda: repository)
    target = uuid4()
    current["ctx"].permissions = frozenset(
        {"users.view", "users.create", "users.manage_access", "users.deactivate", "settings.manage"}
    )
    assert (await client.get("/api/staff/users")).json() == [{"email": "staff@example.test"}]
    assert (await client.get(f"/api/staff/users/{target}/access")).status_code == 200
    invited = await client.post(
        "/api/staff/users/invitations",
        json={
            "email": "invited@example.test",
            "full_name": "Invited Staff",
            "department_id": str(uuid4()),
        },
        headers={"X-CSRF-Token": "known"},
    )
    assert invited.status_code == 202
    repository.snapshot.return_value = None
    assert (await client.get(f"/api/staff/users/{target}/access")).status_code == 404
    spec = {
        "role_ids": [],
        "allow": ["complaints.view"],
        "deny": [],
        "company_ids": [],
        "all_companies": True,
        "ownership": "all",
        "team_ids": [],
    }
    replaced = await client.put(
        f"/api/staff/users/{target}/access", json=spec, headers={"X-CSRF-Token": "known"}
    )
    assert replaced.status_code == 200 and replaced.json()["token_version"] == 4
    assert app.state.redis.delete.await_count == 1
    assert (
        await client.post(
            f"/api/staff/users/{target}/deactivate", headers={"X-CSRF-Token": "known"}
        )
    ).status_code == 200
    assert (
        await client.patch(
            f"/api/staff/users/{target}/2fa-policy",
            json={"required": True},
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 200
    assert (
        await client.patch(
            "/api/staff/settings/staff-2fa",
            json={"required": True},
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 200
    current["ctx"].permissions = frozenset({"users.view"})
    assert (
        await client.patch(
            "/api/staff/settings/staff-2fa",
            json={"required": False},
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 403


async def test_staff_workflows_timeline_and_notifications_scoped(
    api: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, current, service, _auth = api
    repo = SimpleNamespace(
        timeline=AsyncMock(return_value={"events": [], "messages": [], "page": 1}),
        counters=AsyncMock(return_value={"new": 1}),
        attachments=AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=uuid4(),
                    original_filename="proof.pdf",
                    mime_type="application/pdf",
                    size_bytes=42,
                    scan_status="pending",
                )
            ]
        ),
        notifications=AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=uuid4(),
                    kind="created",
                    payload={"ticket_no": "CMP-2026-000001"},
                    created_at=datetime.now(UTC),
                    read_at=None,
                )
            ]
        ),
    )
    monkeypatch.setattr(staff, "ComplaintRepository", lambda: repo)
    current["ctx"].permissions = frozenset(
        {
            "complaints.view",
            "complaints.reply_public",
            "complaints.add_internal_note",
            "complaints.change_status",
            "complaints.view_attachments",
        }
    )
    target = uuid4()
    assert (await client.get("/api/staff/complaints/counters")).json()["new"] == 1
    assert (await client.get(f"/api/staff/complaints/{target}/timeline")).status_code == 200
    assert repo.timeline.call_args.args[1] == current["ctx"].scope
    assert (await client.get(f"/api/staff/complaints/{target}/timeline?page=0")).status_code == 422
    assert (await client.get(f"/api/staff/complaints/{target}/attachments")).status_code == 200
    assert (await client.get("/api/staff/notifications")).status_code == 200
    assert (
        await client.post(
            f"/api/staff/complaints/{target}/reply",
            json={"body": "Public response"},
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/staff/complaints/{target}/notes",
            json={"body": "Internal note"},
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 200
    assert service.reply.call_args.args[4] == "internal"
    assert (
        await client.patch(
            f"/api/staff/complaints/{target}/status",
            json={"status": "resolved"},
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 200
    assert (
        await client.patch(
            f"/api/staff/complaints/{target}/status",
            json={"status": "reopened"},
            headers={"X-CSRF-Token": "known"},
        )
    ).status_code == 422


def test_db_constraint_error_codes_are_not_leaked() -> None:
    from sqlalchemy.exc import DBAPIError

    class DBFailure(Exception):
        def __init__(self, code: str):
            self.sqlstate = code

    for code, expected in [("23514", 409), ("22023", 422), ("42501", 403)]:
        error = DBAPIError("SELECT ...", {}, DBFailure(code))
        actual = staff.access_error(error)
        assert actual.status_code == expected
        assert "SELECT" not in actual.detail


async def test_customer_timeline_reply_feedback_and_visibility(
    api: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, current, _service, _auth = api
    actor = Actor(uuid4(), "customer")
    current["ctx"].actor = actor
    current["ctx"].scope = AccessScope.for_customer(actor)
    repo = SimpleNamespace(
        timeline=AsyncMock(return_value={"events": [], "messages": [], "page": 1}),
        counters=AsyncMock(return_value={"new": 1}),
        attachments=AsyncMock(return_value=[]),
        notifications=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(customer, "ComplaintRepository", lambda: repo)
    target = uuid4()
    path = f"/api/customer/complaints/{target}"
    assert (await client.get("/api/customer/complaints/counters")).json() == {"new": 1}
    assert (await client.get(path + "/timeline")).status_code == 200
    assert repo.timeline.call_args.args[1] == current["ctx"].scope
    assert (await client.get(path + "/attachments")).json() == []
    assert (await client.get("/api/customer/notifications")).json() == []
    assert (await client.post(path + "/reply", json={"body": "Answer"})).status_code == 403
    assert (
        await client.post(
            path + "/reply", json={"body": "Answer"}, headers={"X-CSRF-Token": "known"}
        )
    ).status_code == 200
    assert (
        await client.post(path + "/reopen", headers={"X-CSRF-Token": "known"})
    ).status_code == 200
    assert (
        await client.post(path + "/csat", json={"score": 4}, headers={"X-CSRF-Token": "known"})
    ).status_code == 200


async def test_public_company_metadata_only(api: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _current, _service, _auth = api
    company = uuid4()
    fake_db = SimpleNamespace(
        execute=AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=company,
                    name_en="Insurer",
                    name_ar="شركة",
                    supported_types=["health", "general"],
                )
            ]
        )
    )

    @asynccontextmanager
    async def transaction(factory: object, purpose: str):
        assert purpose == "guest"
        yield fake_db

    monkeypatch.setattr(customer, "purpose_transaction", transaction)
    response = await client.get("/api/companies")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(company),
            "name_en": "Insurer",
            "name_ar": "شركة",
            "insurance_types": ["health", "general"],
        }
    ]
    assert "contact_emails" not in response.text
