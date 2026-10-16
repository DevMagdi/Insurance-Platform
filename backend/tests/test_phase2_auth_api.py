"""Auth HTTP routes set Secure cookies, block CSRF and expose effective /me only."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_api
from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.main import app
from app.security.tokens import TokenManager
from app.services.auth import SessionTokens, StaffLoginResult
from app.settings import Settings


@pytest.fixture
async def fixture(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="test",
    )
    token_manager = TokenManager(settings)
    actor = Actor(uuid4(), "staff")
    sid = uuid4()
    tokens = SessionTokens(
        token_manager.issue(actor, sid, 0, "access", timedelta(minutes=15)),
        token_manager.issue(actor, sid, 0, "refresh", timedelta(days=7)),
    )
    service = SimpleNamespace(
        settings=settings,
        tokens=token_manager,
        start_customer_otp=AsyncMock(),
        verify_customer=AsyncMock(return_value=tokens),
        staff_login=AsyncMock(return_value=StaffLoginResult(tokens, None, None)),
        change_forced_password=AsyncMock(),
        verify_staff_totp=AsyncMock(return_value=tokens),
        enroll_start=AsyncMock(return_value="otpauth://totp/Insurance%20Platform"),
        enroll_confirm=AsyncMock(),
        accept_invitation=AsyncMock(),
        limits=SimpleNamespace(check=AsyncMock()),
        refresh=AsyncMock(return_value=tokens),
    )
    app.state.auth = service
    db = SimpleNamespace()
    ctx = RequestContext(
        db,
        actor,
        sid,
        0,
        AccessScope(actor),
        frozenset(),
        {"id": str(actor.id), "permissions": ["complaints.view"]},
        SimpleNamespace(),
    )
    mock_repo = SimpleNamespace(
        get_session=AsyncMock(
            return_value=SimpleNamespace(
                id=sid, revoked_at=None, subject_id=actor.id, subject_kind=actor.scope
            )
        ),
        list_sessions=AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=sid,
                    created_at=datetime.now(UTC),
                    last_used_at=None,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    ip_address="127.0.0.1",
                    user_agent="Browser",
                )
            ]
        ),
        revoke_all=AsyncMock(),
    )
    monkeypatch.setattr(auth_api, "AuthRepository", lambda: mock_repo)

    async def override_context():
        yield ctx

    app.dependency_overrides[authenticated_context] = override_context
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        yield client, ctx, service, mock_repo, tokens
    app.dependency_overrides.clear()


async def test_csrf_bootstrap_migrates_legacy_cookie_for_the_spa(fixture: tuple) -> None:
    client, _ctx, _service, _repo, _tokens = fixture

    empty = await client.get("/api/auth/csrf")
    assert empty.status_code == 200
    assert empty.json() == {"available": False}
    assert empty.headers["cache-control"] == "no-store"
    assert not empty.headers.get_list("set-cookie")

    client.cookies.set("csrf_token", "legacy-csrf", path="/api")
    migrated = await client.get("/api/auth/csrf")
    assert migrated.status_code == 200
    assert migrated.json() == {"available": True}
    cookie_headers = migrated.headers.get_list("set-cookie")
    assert any(
        "csrf_token=legacy-csrf" in cookie and "Path=/" in cookie and "Secure" in cookie
        for cookie in cookie_headers
    )
    assert any(
        "csrf_token=" in cookie and "Path=/api" in cookie and "Max-Age=0" in cookie
        for cookie in cookie_headers
    )
    assert client.cookies.get("csrf_token", path="/") == "legacy-csrf"


async def test_staff_login_uses_secure_http_only_cookies_and_no_password_echo(
    fixture: tuple,
) -> None:
    client, _ctx, _service, _repo, _tokens = fixture
    result = await client.post(
        "/api/auth/staff/login",
        json={"email": "staff@example.test", "password": "long-password-123"},
    )
    assert result.status_code == 200, result.text
    assert result.json() == {"next_step": "authenticated", "challenge": None}
    cookie_headers = result.headers.get_list("set-cookie")
    assert any(
        "access_token=" in cookie and "HttpOnly" in cookie and "Secure" in cookie
        for cookie in cookie_headers
    )
    assert any(
        "refresh_token=" in cookie and "Path=/api/auth" in cookie for cookie in cookie_headers
    )
    assert "long-password-123" not in result.text
    assert (await client.get("/api/me")).json()["access"]["permissions"] == ["complaints.view"]


async def test_otp_verify_and_mfa_challenge_do_not_set_cookies_early(fixture: tuple) -> None:
    client, _ctx, service, _repo, _tokens = fixture
    result = await client.post("/api/auth/customer/otp/start", json={"mobile": "+201123456789"})
    assert result.status_code == 202
    assert set(result.json()) == {"message"}
    response = await client.post(
        "/api/auth/customer/otp/verify", json={"mobile": "+201123456789", "code": "123456"}
    )
    assert response.status_code == 200 and response.json()["authenticated_as"] == "customer"
    service.staff_login.return_value = StaffLoginResult(None, "limited-challenge", "totp_verify")
    result = await client.post(
        "/api/auth/staff/login",
        json={"email": "staff@example.test", "password": "long-password-123"},
    )
    assert result.json()["next_step"] == "totp_verify"
    assert all("Max-Age=0" in cookie for cookie in result.headers.get_list("set-cookie"))


async def test_refresh_csrf_rotation_and_logout_revocation(fixture: tuple) -> None:
    client, _ctx, service, repo, _tokens = fixture
    result = await client.post(
        "/api/auth/staff/login",
        json={"email": "staff@example.test", "password": "long-password-123"},
    )
    assert result.status_code == 200
    response = await client.post("/api/auth/refresh")
    assert response.status_code == 403
    csrf = client.cookies.get("csrf_token")
    response = await client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and service.refresh.await_count == 1
    service.refresh.return_value = None
    csrf = client.cookies.get("csrf_token")
    response = await client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 401
    assert any(
        "refresh_token=" in header and "Max-Age=0" in header
        for header in response.headers.get_list("set-cookie")
    )
    result = await client.post(
        "/api/auth/staff/login",
        json={"email": "staff@example.test", "password": "long-password-123"},
    )
    assert result.status_code == 200
    csrf = client.cookies.get("csrf_token")
    assert (await client.post("/api/auth/logout")).status_code == 403
    response = await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and repo.get_session.await_count == 1
    assert repo.get_session.return_value.revoked_at is not None


async def test_self_sessions_and_voluntary_mfa_revoke(fixture: tuple) -> None:
    client, ctx, _service, repo, _tokens = fixture
    response = await client.get("/api/me/sessions")
    assert response.status_code == 200 and response.json()[0]["current"]
    client.cookies.set("csrf_token", "known", path="/api")
    response = await client.post("/api/me/2fa/enroll/start", headers={"X-CSRF-Token": "known"})
    assert response.status_code == 200 and "otpauth://" in response.text
    response = await client.post(
        "/api/me/2fa/enroll/confirm", json={"code": "123456"}, headers={"X-CSRF-Token": "known"}
    )
    assert response.status_code == 200 and "sign_in" in response.text
    response = await client.delete(
        f"/api/me/sessions/{ctx.session_id}", headers={"X-CSRF-Token": "known"}
    )
    assert response.status_code == 200
    response = await client.post("/api/me/sessions/revoke-all", headers={"X-CSRF-Token": "known"})
    assert response.status_code == 200 and repo.revoke_all.await_count == 1
