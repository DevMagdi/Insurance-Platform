"""Hermetic onboarding tests: real-address routing is NOT an inbox delivery claim."""

from __future__ import annotations

import base64
import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import ValidationError

from app.api import auth as auth_api
from app.api import staff as staff_api
from app.api.deps import RequestContext, authenticated_context
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.domain.schemas import (
    DepartmentSpec,
    IntegrationConfigSpec,
    InviteAcceptRequest,
    StaffPersonalData,
)
from app.middleware.body_limit import BodySizeLimitMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.security.blobs import AVATAR, STAFF_PROFILE, BlobCipher
from app.services.auth import AuthService
from app.services.integrations import config_aad
from app.services.providers import ProviderUnavailable, SMTPDelivery
from app.services.staff_mail import StaffMailService, invitation_url
from app.services.staff_profiles import AVATAR_MAX_BYTES, StaffProfileService, normalize_avatar
from app.settings import Settings

PROFILE = {
    "full_name": "Mona Ahmed Ali",
    "national_id": "29901010112345",
    "mobile": "+201123456789",
    "address": "12 Nile Street, Dokki",
    "city": "Giza",
    "job_title": "Claims specialist",
    "emergency_phone": "+201012345678",
}
PASSWORD = "Cedar-River-Garden-49"


def photo(format: str = "PNG", size: tuple[int, int] = (32, 32)) -> str:
    data = io.BytesIO()
    image = Image.new("RGB", size, "blue")
    exif = Image.Exif()
    exif[0x010E] = "Private location metadata"
    image.save(data, format=format, exif=exif)
    return base64.b64encode(data.getvalue()).decode()


def settings(**overrides: object) -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="test",
        **overrides,
    )


def test_department_name_description_and_state_are_validated() -> None:
    assert DepartmentSpec(name="  Claims  ").model_dump() == {
        "name": "Claims",
        "description": "",
        "is_active": True,
    }
    assert DepartmentSpec(
        name=" Claims ", description="  Follow-up ", is_active=False
    ).model_dump() == {
        "name": "Claims",
        "description": "Follow-up",
        "is_active": False,
    }
    for invalid in (
        {"name": "   "},
        {"name": "A"},
        {"name": "Claims", "description": "x" * 501},
        {"name": "Claims", "unknown": True},
    ):
        with pytest.raises(ValidationError):
            DepartmentSpec.model_validate(invalid)


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_photo_is_decoded_resized_and_stripped_of_metadata(format: str) -> None:
    clean = normalize_avatar(photo(format, (900, 500)))
    with Image.open(io.BytesIO(clean)) as image:
        assert image.format == "JPEG" and max(image.size) <= 512
        assert not image.getexif()
        assert b"Private location" not in clean


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "%%%",
        base64.b64encode(b"<svg><script/></svg>").decode(),
        base64.b64encode(b"x" * (AVATAR_MAX_BYTES + 1)).decode(),
        photo("GIF"),
    ],
)
def test_photo_rejects_non_images_oversize_and_unsafe_types(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_avatar(bad)


def test_photo_refuses_excessive_decoded_dimensions() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        normalize_avatar(photo("PNG", (4100, 4000)))


@pytest.mark.parametrize(
    "changes",
    [
        {"national_id": "12345678901234"},
        {"national_id": "29902310112345"},
        {"mobile": "123"},
        {"address": "x"},
        {"city": ""},
        {"emergency_phone": "123"},
        {"department_id": str(uuid4())},
    ],
)
def test_personal_data_validation_and_mass_assignment(changes: dict) -> None:
    with pytest.raises(ValidationError):
        StaffPersonalData.model_validate(PROFILE | changes)


async def test_workplace_provider_uses_actual_address_never_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = settings()
    mail = StaffMailService(config, SimpleNamespace(send_invitation=AsyncMock()))
    credentials = {"from_email": "admin@company.com", "sandbox_recipient": "sandbox@example.test"}
    ciphertext = mail.cipher.encrypt_json(
        credentials, associated_data=config_aad(None, "gmail_oauth")
    )
    mail.repository.email_connection = AsyncMock(
        return_value={
            "provider": "gmail_oauth",
            "config_ciphertext": ciphertext,
            "connection_status": "connected",
        }
    )
    sender = SimpleNamespace(send=AsyncMock())

    def build(_provider: str, _config: dict):
        return sender

    monkeypatch.setattr("app.services.staff_mail.build_provider", build)
    url = invitation_url(config, "a" * 43)
    for recipient in ("new.employee@gmail.com", "new.employee@company.com"):
        await mail.send(SimpleNamespace(), recipient, url)
        request = sender.send.call_args.args[0]
        assert request.recipients == (recipient,)
        assert url in request.text_body and "48 hours" in request.text_body
        assert "sandbox@example.test" not in request.text_body
        assert "This is an automated message" in request.text_body
        assert "/api/public/brand/logo" not in request.html_body  # CID-only logo; no remote img.
        assert 'dir="rtl"' in request.html_body
        assert "a" * 43 not in sender.send.call_args.kwargs["idempotency_key"]
    mail.fallback.send_invitation.assert_not_awaited()
    sender.send.side_effect = RuntimeError("provider rejected")
    with pytest.raises(RuntimeError):
        await mail.send(SimpleNamespace(), "new.employee@gmail.com", url)
    mail.fallback.send_invitation.assert_not_awaited()
    mail.repository.email_connection.return_value["connection_status"] = "failed"
    with pytest.raises(ProviderUnavailable, match="Reconnect"):
        await mail.send(SimpleNamespace(), "new.employee@gmail.com", url)


async def test_smtp_fallback_sends_to_requested_envelope_and_supports_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = settings(
        SMTP_HOST="smtp.company.com",
        SMTP_FROM="admin@company.com",
        SMTP_PORT=465,
        SMTP_USE_TLS=True,
        SMTP_START_TLS=False,
    )
    sent = AsyncMock()
    monkeypatch.setattr("app.services.providers.aiosmtplib.send", sent)
    mail = StaffMailService(config, SMTPDelivery(config))
    mail.repository.email_connection = AsyncMock(return_value=None)
    await mail.send(
        SimpleNamespace(), "new.employee@gmail.com", "https://app.company.com/invite#token=opaque"
    )
    assert sent.call_args.args[0]["To"] == "new.employee@gmail.com"
    assert sent.call_args.kwargs["use_tls"] is True
    assert sent.call_args.kwargs["start_tls"] is False
    await mail.send(
        SimpleNamespace(),
        "employee@company.com",
        "https://app.company.com/invite#token=reset",
        reset=True,
    )
    assert "Reset" in sent.call_args.args[0]["Subject"]


@pytest.mark.parametrize(
    "host",
    ["localhost", "mailpit", "sandbox.smtp.mailtrap.io", "127.0.0.1", "smtp.local", "10.1.1.1"],
)
async def test_production_cannot_silently_use_a_development_mail_sink(host: str) -> None:
    config = settings(SMTP_HOST=host, SMTP_FROM="admin@company.com", SMTP_START_TLS=True)
    config = config.model_copy(update={"environment": "production"})
    delivery = SimpleNamespace(send_invitation=AsyncMock())
    mail = StaffMailService(config, delivery)
    mail.repository.email_connection = AsyncMock(return_value=None)
    with pytest.raises(ProviderUnavailable):
        await mail.send(SimpleNamespace(), "employee@gmail.com", "https://app.company.com/invite")
    delivery.send_invitation.assert_not_awaited()


def test_production_requires_real_public_link_and_transport_security() -> None:
    config = settings().model_copy(update={"environment": "production"})
    for url in (
        "http://localhost:8080",
        "http://app.company.com",
        "https://example.test",
        "https://app.company.com/path",
        "https://user:secret@app.company.com",
        "https://app.company.com?next=bad",
    ):
        with pytest.raises(ProviderUnavailable):
            invitation_url(config.model_copy(update={"public_app_url": url}), "t" * 43)
    assert (
        invitation_url(
            config.model_copy(update={"public_app_url": "https://app.company.com/"}), "t" * 43
        )
        == "https://app.company.com/invite#token=" + "t" * 43
    )
    mail = StaffMailService(config, SimpleNamespace())
    with pytest.raises(ProviderUnavailable, match="TLS"):
        mail._validate_transport(
            {"host": "smtp.company.com", "from_email": "admin@company.com", "start_tls": False}
        )
    with pytest.raises(ProviderUnavailable, match="sender"):
        mail._validate_transport(
            {
                "host": "smtp.company.com",
                "from_email": "no-reply@insurance.local",
                "start_tls": True,
            }
        )
    with pytest.raises(ValidationError):
        settings(SMTP_USE_TLS=True, SMTP_START_TLS=True)
    with pytest.raises(ValidationError):
        IntegrationConfigSpec(provider="smtp", credentials={"start_tls": "true", "use_tls": "true"})


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch):
    redis = fakeredis.aioredis.FakeRedis()
    auth = AuthService(SimpleNamespace(), redis, settings(), SimpleNamespace())
    user_id = uuid4()
    auth.repo = SimpleNamespace(
        invitation_details=AsyncMock(
            return_value={
                "id": str(user_id),
                "kind": "invitation",
                "email": "employee@gmail.com",
                "full_name": "Employee",
                "department_name": "Claims",
            }
        ),
        complete_invite=AsyncMock(return_value=user_id),
    )

    @asynccontextmanager
    async def transaction(_factory: object, purpose: str):
        assert purpose == "invitation"
        yield SimpleNamespace()

    monkeypatch.setattr("app.services.auth.purpose_transaction", transaction)
    yield auth, user_id
    await redis.aclose()


async def test_acceptance_requires_profile_and_photo_and_binds_encryption_to_invited_user(
    service: tuple,
) -> None:
    auth, user_id = service
    profile = StaffPersonalData.model_validate(PROFILE)
    for personal, avatar in ((None, None), (profile, None), (None, photo())):
        with pytest.raises(HTTPException) as exc:
            await auth.accept_invitation("t" * 43, PASSWORD, personal, avatar)
        assert exc.value.status_code == 422
    with pytest.raises(ValueError):
        await auth.accept_invitation("t" * 43, PASSWORD, profile, "not-an-image")
    auth.repo.complete_invite.assert_not_awaited()
    await auth.accept_invitation("t" * 43, PASSWORD, profile, photo())
    args = auth.repo.complete_invite.call_args.kwargs
    assert args["user_id"] == user_id and args["name"] == PROFILE["full_name"]
    assert PROFILE["national_id"].encode() not in args["personal"]
    decoded = json.loads(
        auth.staff_profiles.blobs.decrypt(
            args["personal"], purpose=STAFF_PROFILE, resource_id=user_id
        )
    )
    assert decoded == PROFILE
    assert args["avatar"][:3] == b"KR\x01"
    assert await auth.passwords.verify(args["password_hash"], PASSWORD)
    with pytest.raises(ValueError):
        auth.staff_profiles.blobs.decrypt(args["avatar"], purpose=AVATAR, resource_id=uuid4())
    inspected = await auth.inspect_invitation("t" * 43)
    assert "id" not in inspected and inspected["email"] == "employee@gmail.com"
    auth.repo.invitation_details.return_value = None
    with pytest.raises(HTTPException) as exc:
        await auth.inspect_invitation("t" * 43)
    assert exc.value.status_code == 400


async def test_reset_never_overwrites_profile(service: tuple) -> None:
    auth, _user_id = service
    auth.repo.invitation_details.return_value["kind"] = "password_reset"
    await auth.accept_invitation("t" * 43, PASSWORD)
    args = auth.repo.complete_invite.call_args.kwargs
    assert args["personal"] is args["avatar"] is args["blind"] is None
    with pytest.raises(HTTPException) as exc:
        await auth.accept_invitation(
            "t" * 43, PASSWORD, StaffPersonalData.model_validate(PROFILE), photo()
        )
    assert exc.value.status_code == 422


async def test_profile_and_avatar_reads_decrypt_only_authorized_rows() -> None:
    user = uuid4()
    cipher = BlobCipher(b"e" * 32)
    repository = SimpleNamespace(
        profile=AsyncMock(
            return_value={
                "metadata": {"id": str(user), "full_name": "Updated Name", "has_avatar": True},
                "personal_ciphertext": cipher.encrypt(
                    json.dumps(PROFILE).encode(), purpose=STAFF_PROFILE, resource_id=user
                ),
            }
        ),
        avatar=AsyncMock(
            return_value=cipher.encrypt(b"normalized JPEG", purpose=AVATAR, resource_id=user)
        ),
    )
    service = StaffProfileService(cipher, repository)
    result = await service.profile(SimpleNamespace(), user)
    assert result["personal"]["national_id"] == PROFILE["national_id"]
    assert result["personal"]["date_of_birth"] == "1999-01-01"
    assert result["personal"]["full_name"] == "Updated Name"
    assert result["avatar_url"] == f"/api/staff/users/{user}/avatar"
    assert await service.avatar(SimpleNamespace(), user) == b"normalized JPEG"
    repository.profile.return_value = {
        "metadata": {"has_avatar": False},
        "personal_ciphertext": None,
    }
    assert (await service.profile(SimpleNamespace(), user))["personal"] is None
    repository.profile.return_value = repository.avatar.return_value = None
    for action in (service.profile, service.avatar):
        with pytest.raises(HTTPException) as exc:
            await action(SimpleNamespace(), user)
        assert exc.value.status_code == 404


async def test_api_permissions_csrf_validation_and_private_photo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(auth_api.router)
    app.include_router(staff_api.router)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, all_companies=True, ownership=OwnershipLevel.ALL)
    ctx = RequestContext(
        SimpleNamespace(), actor, uuid4(), 0, scope, frozenset(), {}, SimpleNamespace()
    )
    profiles = SimpleNamespace(
        profile=AsyncMock(return_value={"full_name": "Employee"}),
        avatar=AsyncMock(return_value=b"jpeg"),
    )
    auth = SimpleNamespace(
        staff_profiles=profiles,
        create_invitation=AsyncMock(return_value=uuid4()),
        limits=SimpleNamespace(check=AsyncMock()),
        inspect_invitation=AsyncMock(return_value={"email": "staff@gmail.com"}),
        accept_invitation=AsyncMock(),
        resend_invitation=AsyncMock(),
    )
    app.state.auth = auth
    repo = SimpleNamespace(
        departments=AsyncMock(return_value=[]),
        create_department=AsyncMock(return_value=uuid4()),
        update_department=AsyncMock(return_value=True),
        assign_department=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(staff_api, "StaffProfileRepository", lambda: repo)

    async def context():
        yield ctx

    app.dependency_overrides[authenticated_context] = context
    target = uuid4()
    headers = {"X-CSRF-Token": "known"}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
        cookies={"csrf_token": "known"},
    ) as client:
        assert (await client.get(f"/api/staff/users/{target}/profile")).status_code == 403
        assert (await client.get(f"/api/staff/users/{target}/avatar")).status_code == 403
        profiles.profile.assert_not_awaited()
        ctx.permissions = frozenset({"users.view", "users.create", "users.edit"})
        assert (await client.get(f"/api/staff/users/{target}/profile")).json()[
            "full_name"
        ] == "Employee"
        result = await client.get(f"/api/staff/users/{target}/avatar")
        assert result.content == b"jpeg" and result.headers["content-type"] == "image/jpeg"
        assert "no-store" in result.headers["cache-control"]
        assert (await client.get("/api/staff/departments")).json() == []
        assert (
            await client.post("/api/staff/departments", json={"name": "Claims"})
        ).status_code == 403
        created = await client.post(
            "/api/staff/departments",
            json={"name": "  Claims  ", "description": "  Claims team  ", "is_active": False},
            headers=headers,
        )
        assert created.status_code == 201
        assert created.json()["name"] == "Claims" and created.json()["is_active"] is False
        repo.create_department.assert_awaited_once_with(ctx.db, "Claims", "Claims team", False)
        for body in ({"name": "  "}, {"name": "Claims", "description": "x" * 501}):
            assert (
                await client.post("/api/staff/departments", json=body, headers=headers)
            ).status_code == 422
        department_id = uuid4()
        path = f"/api/staff/departments/{department_id}"
        assert (await client.put(path, json={"name": "Claims"})).status_code == 403
        edited = await client.put(
            path,
            json={"name": "Support", "description": "Team", "is_active": True},
            headers=headers,
        )
        assert edited.status_code == 200 and edited.json()["description"] == "Team"
        repo.update_department.assert_awaited_once_with(
            ctx.db, department_id, "Support", "Team", True
        )
        repo.update_department.return_value = False
        assert (
            await client.put(path, json={"name": "Support"}, headers=headers)
        ).status_code == 404
        ctx.permissions = frozenset({"users.view", "users.create"})
        assert (
            await client.put(path, json={"name": "Support"}, headers=headers)
        ).status_code == 403
        ctx.permissions = frozenset({"users.view", "users.create", "users.edit"})
        assert (
            await client.patch(
                f"/api/staff/users/{target}/department",
                json={"department_id": str(uuid4())},
                headers=headers,
            )
        ).status_code == 200
        payload = {
            "email": "employee@gmail.com",
            "full_name": "Employee",
            "department_id": str(uuid4()),
        }
        assert (
            await client.post("/api/staff/users/invitations", json=payload, headers=headers)
        ).status_code == 202
        assert (
            await client.post(
                "/api/staff/users/invitations",
                json={"email": "employee@gmail.com", "full_name": "Employee"},
                headers=headers,
            )
        ).status_code == 422
        assert (
            await client.post(f"/api/staff/users/{target}/resend-invitation", headers=headers)
        ).status_code == 202
        inspect = await client.post("/api/auth/staff/invitation/inspect", json={"token": "t" * 43})
        assert inspect.status_code == 200 and "no-store" in inspect.headers["cache-control"]
        acceptance = {
            "token": "t" * 43,
            "password": PASSWORD,
            "profile": PROFILE,
            "avatar_base64": photo(),
        }
        assert (
            await client.post("/api/auth/staff/invitation/accept", json=acceptance)
        ).status_code == 200
        assert auth.accept_invitation.call_args.args[2] == StaffPersonalData.model_validate(PROFILE)
        acceptance["avatar_base64"] = "x" * 1_050_000
        assert (
            await client.post("/api/auth/staff/invitation/accept", json=acceptance)
        ).status_code == 413
    assert (
        InviteAcceptRequest.model_validate({"token": "t" * 43, "password": PASSWORD}).profile
        is None
    )
