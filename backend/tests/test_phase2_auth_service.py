"""Auth service tests: OTP, forced password, MFA, rotation/replay, invitations."""

from __future__ import annotations

import base64
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import fakeredis.aioredis
import pyotp
import pytest
from fastapi import HTTPException

from app.db import Actor
from app.domain.schemas import CustomerVerifyRequest
from app.repositories.auth import AuthRepository, StaffCredentials
from app.services.auth import AuthService
from app.services.providers import ProviderUnavailable
from app.settings import Settings

NID = "29901010112345"
MOBILE = "+201123456789"
PASSWORD = "safe-password-123"


class CapturingDelivery:
    def __init__(self) -> None:
        self.codes: dict[str, str] = {}
        self.invites: dict[str, str] = {}
        self.fail_otp = False
        self.fail_invite = False

    async def send_otp(self, email: str, code: str) -> None:
        if self.fail_otp:
            raise ProviderUnavailable("No SMS provider")
        self.codes[email] = code

    async def send_invitation(self, email: str, url: str) -> None:
        if self.fail_invite:
            raise ProviderUnavailable("No SMTP provider")
        self.invites[email] = url

    async def send_notice(
        self, mobile: str, email: str | None, kind: str, payload: dict[str, str]
    ) -> bool:
        return False


@pytest.fixture
async def fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    AuthService,
    Mock,
    CapturingDelivery,
    dict,
    fakeredis.aioredis.FakeRedis,
]:
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="test",
    )
    redis = fakeredis.aioredis.FakeRedis()
    delivery = CapturingDelivery()
    service = AuthService(SimpleNamespace(), redis, settings, delivery)
    repo = Mock(spec=AuthRepository)
    for name in (
        "staff_by_email",
        "staff_by_id",
        "customers_by_mobile",
        "customer_email_matches",
        "register_customer",
        "current_version",
        "create_session",
        "get_session",
        "revoke_family",
        "record_login",
        "change_password",
        "set_totp",
        "invite",
        "accept_invite",
        "invitation_details",
        "complete_invite",
    ):
        setattr(repo, name, AsyncMock())
    service.repo = repo
    service.staff_mail.repository.email_connection = AsyncMock(return_value=None)
    db = SimpleNamespace()
    scopes: dict[str, list] = {"purpose": [], "scoped": []}

    @asynccontextmanager
    async def fake_purpose(factory: object, purpose: str):
        scopes["purpose"].append(purpose)
        yield db

    @asynccontextmanager
    async def fake_scoped(factory: object, actor: Actor):
        scopes["scoped"].append(actor)
        yield db

    monkeypatch.setattr("app.services.auth.purpose_transaction", fake_purpose)
    monkeypatch.setattr("app.services.auth.scoped_transaction", fake_scoped)
    repo.current_version.return_value = 0
    repo.customers_by_mobile.return_value = [(uuid4(), 0)]
    repo.customer_email_matches.return_value = True
    repo.invite.return_value = uuid4()
    repo.register_customer.return_value = uuid4()
    repo.accept_invite.return_value = uuid4()
    repo.complete_invite.return_value = repo.invite.return_value
    repo.invitation_details.return_value = {
        "id": str(repo.invite.return_value),
        "kind": "password_reset",
    }
    yield service, repo, delivery, scopes, redis
    await redis.aclose()


def staff(**overrides: object) -> StaffCredentials:
    fields = dict(
        id=uuid4(),
        email="agent@example.test",
        full_name="Agent",
        password_hash=None,
        status="active",
        must_change_password=False,
        two_factor_enabled=False,
        two_factor_required=False,
        totp_secret_ciphertext=None,
        token_version=0,
        global_2fa_required=False,
    )
    fields.update(overrides)
    return StaffCredentials(**fields)


async def test_otp_login_new_registration_and_replay(fixture: tuple) -> None:
    auth, repo, delivery, scopes, _redis = fixture
    await auth.start_customer_otp(MOBILE, "customer@example.test", "127.0.0.1")
    code = delivery.codes["customer@example.test"]
    with pytest.raises(HTTPException) as exc:
        await auth.start_customer_otp(MOBILE, "customer@example.test", "127.0.0.1")
    assert exc.value.status_code == 429
    item = CustomerVerifyRequest(mobile=MOBILE, email="customer@example.test", code=code)
    tokens = await auth.verify_customer(item, "127.0.0.1", "Browser")
    assert auth.tokens.parse(tokens.access, "access").subject.scope == "customer"
    assert scopes["purpose"] == ["customer_auth"]
    repo.register_customer.assert_not_awaited()
    with pytest.raises(HTTPException):
        await auth.verify_customer(item, "127.0.0.1", "Browser")
    next_mobile = "+201155555555"
    await auth.start_customer_otp(next_mobile, "new@example.test", "127.0.0.1")
    repo.customers_by_mobile.return_value = []
    registration_code = delivery.codes["new@example.test"]
    with pytest.raises(HTTPException) as exc:
        await auth.verify_customer(
            CustomerVerifyRequest(
                mobile=next_mobile, email="new@example.test", code=registration_code
            ),
            "127.0.0.1",
            "Browser",
        )
    assert exc.value.status_code == 409
    assert await _redis.exists(auth.otp._key(f"{next_mobile}:new@example.test"))

    registration = CustomerVerifyRequest(
        mobile=next_mobile,
        code=registration_code,
        email="new@example.test",
        national_id=NID,
        full_name="New Customer",
    )
    await auth.verify_customer(registration, "127.0.0.1", "Browser")
    assert repo.register_customer.await_count == 1
    assert repo.register_customer.call_args.kwargs["last4"] == NID[-4:]
    assert len(repo.register_customer.call_args.kwargs["blind"]) == 32
    assert not await _redis.exists(auth.otp._key(f"{next_mobile}:new@example.test"))


async def test_customer_otp_is_rate_limited_by_email(fixture: tuple) -> None:
    auth, _repo, _delivery, _scopes, _redis = fixture
    email = "customer@example.test"
    for mobile in ("+201155555555", "+201166666666", "+201177777777"):
        await auth.start_customer_otp(mobile, email, "127.0.0.1")
    with pytest.raises(HTTPException) as exc:
        await auth.start_customer_otp("+201188888888", email, "127.0.0.1")
    assert exc.value.status_code == 429


async def test_otp_multi_profile_and_provider_failure(fixture: tuple) -> None:
    auth, repo, delivery, _scopes, redis = fixture
    delivery.fail_otp = True
    with pytest.raises(HTTPException) as exc:
        await auth.start_customer_otp(MOBILE, "customer@example.test", "127.0.0.1")
    assert exc.value.status_code == 503
    assert not await redis.exists(auth.otp._key(f"{MOBILE}:customer@example.test"))
    delivery.fail_otp = False
    await auth.start_customer_otp(MOBILE, "customer@example.test", "127.0.0.1")
    customer_id = uuid4()
    repo.customers_by_mobile.return_value = [(customer_id, 0), (uuid4(), 0)]
    code = delivery.codes["customer@example.test"]
    with pytest.raises(HTTPException) as exc:
        await auth.verify_customer(
            CustomerVerifyRequest(mobile=MOBILE, email="customer@example.test", code=code),
            "127.0.0.1",
            "Agent",
        )
    assert exc.value.status_code == 409
    assert await redis.exists(auth.otp._key(f"{MOBILE}:customer@example.test"))

    repo.customers_by_mobile.return_value = [(customer_id, 0)]
    tokens = await auth.verify_customer(
        CustomerVerifyRequest(
            mobile=MOBILE,
            email="customer@example.test",
            code=code,
            national_id=NID,
            full_name="Customer",
        ),
        "127.0.0.1",
        "Agent",
    )
    assert auth.tokens.parse(tokens.access, "access").subject.id == customer_id
    assert not await redis.exists(auth.otp._key(f"{MOBILE}:customer@example.test"))


async def test_staff_unknown_disabled_forced_password_and_login(fixture: tuple) -> None:
    auth, repo, _delivery, scopes, _redis = fixture
    repo.staff_by_email.return_value = None
    with pytest.raises(HTTPException) as exc:
        await auth.staff_login("absent@example.test", PASSWORD, "127.0.0.1", "Browser")
    assert exc.value.status_code == 401
    password_hash = await auth.passwords.hash(PASSWORD)
    account = staff(password_hash=password_hash, status="disabled")
    repo.staff_by_email.return_value = account
    with pytest.raises(HTTPException):
        await auth.staff_login(account.email, PASSWORD, "127.0.0.1", "Browser")
    account = staff(password_hash=password_hash, must_change_password=True)
    repo.staff_by_email.return_value = account
    repo.staff_by_id.return_value = account
    outcome = await auth.staff_login(account.email, PASSWORD, "127.0.0.1", "Browser")
    assert outcome.next_step == "password_change" and outcome.tokens is None
    repo.change_password.return_value = True
    await auth.change_forced_password(outcome.challenge, "another-password-123")
    repo.change_password.assert_awaited_once()
    with pytest.raises(HTTPException):
        await auth.change_forced_password(outcome.challenge, "another-password-123")
    fresh = staff(id=account.id, password_hash=password_hash)
    repo.staff_by_email.return_value = fresh
    outcome = await auth.staff_login(fresh.email, PASSWORD, "127.0.0.1", "Browser")
    assert outcome.tokens and outcome.next_step is None
    assert scopes["scoped"][-1] == Actor(fresh.id, "staff")
    repo.record_login.assert_awaited_once()


async def test_totp_policy_required_enroll_confirm_verify(fixture: tuple) -> None:
    auth, repo, _delivery, _scopes, redis = fixture
    hashed = await auth.passwords.hash(PASSWORD)
    account = staff(password_hash=hashed, two_factor_required=True)
    repo.staff_by_email.return_value = account
    repo.staff_by_id.return_value = account
    login = await auth.staff_login(account.email, PASSWORD, "127.0.0.1", "Browser")
    assert login.next_step == "totp_setup"
    claims = auth.tokens.parse(login.challenge, "challenge")
    uri = await auth.enroll_start(
        claims.subject, claims.session_id, claims.token_version, login.challenge
    )
    secret = parse_qs(urlsplit(uri).query)["secret"][0]
    repo.set_totp.return_value = True
    await auth.enroll_confirm(
        claims.subject, claims.session_id, 0, pyotp.TOTP(secret).now(), login.challenge
    )
    repo.set_totp.assert_awaited_once()
    assert not await redis.exists(f"auth:enroll:{account.id}:{claims.session_id}")
    with pytest.raises(HTTPException):
        await auth.enroll_confirm(
            claims.subject, claims.session_id, 0, pyotp.TOTP(secret).now(), login.challenge
        )
    account = staff(
        id=account.id,
        password_hash=hashed,
        two_factor_enabled=True,
        two_factor_required=True,
        totp_secret_ciphertext=auth.totp.encrypt(secret),
    )
    repo.staff_by_email.return_value = account
    repo.staff_by_id.return_value = account
    challenge = await auth.staff_login(account.email, PASSWORD, "127.0.0.1", "Browser")
    assert challenge.next_step == "totp_verify"
    with pytest.raises(HTTPException):
        await auth.verify_staff_totp(
            challenge.challenge,
            "000000" if pyotp.TOTP(secret).now() != "000000" else "111111",
            "127.0.0.1",
            "Browser",
        )
    result = await auth.verify_staff_totp(
        challenge.challenge, pyotp.TOTP(secret).now(), "127.0.0.1", "Browser"
    )
    assert auth.tokens.parse(result.access, "access").subject.id == account.id
    with pytest.raises(HTTPException):
        await auth.verify_staff_totp(
            challenge.challenge, pyotp.TOTP(secret).now(), "127.0.0.1", "Browser"
        )


async def test_refresh_rotation_replay_revokes_family(fixture: tuple) -> None:
    auth, repo, _delivery, _scopes, _redis = fixture
    actor = Actor(uuid4(), "staff")
    sessions: dict = {}

    async def create_session(db: object, **kwargs: object):
        record = SimpleNamespace(
            id=kwargs["sid"],
            family_id=kwargs["family_id"],
            refresh_digest=kwargs["digest"],
            token_version=kwargs["version"],
            expires_at=kwargs["expires_at"],
            revoked_at=None,
            last_used_at=None,
            subject_id=kwargs["actor"].id,
            subject_kind="staff",
            rotated_from_id=kwargs.get("rotated_from"),
        )
        sessions[record.id] = record
        return record

    async def get_session(db: object, sid: object, **kwargs: object):
        return sessions.get(sid)

    async def revoke_family(db: object, family_id: object):
        for record in sessions.values():
            if record.family_id == family_id:
                record.revoked_at = datetime.now(UTC)

    repo.create_session.side_effect = create_session
    repo.get_session.side_effect = get_session
    repo.revoke_family.side_effect = revoke_family
    old = await auth.issue_session(actor, 0, "127.0.0.1", "Browser")
    rotated = await auth.refresh(old.refresh, "127.0.0.1", "New Browser")
    assert rotated is not None and rotated.refresh != old.refresh
    original_id = auth.tokens.parse(old.access, "access").session_id
    new_id = auth.tokens.parse(rotated.access, "access").session_id
    assert sessions[original_id].revoked_at is not None
    assert sessions[new_id].rotated_from_id == original_id
    assert await auth.refresh(old.refresh, "127.0.0.1", "Browser") is None
    assert sessions[new_id].revoked_at is not None
    assert await auth.refresh(rotated.refresh, "127.0.0.1", "Browser") is None


async def test_email_invitation_token_is_hashed_and_single_use(fixture: tuple) -> None:
    auth, repo, delivery, _scopes, _redis = fixture
    user = await auth.create_invitation(
        SimpleNamespace(), "INVITED@example.test", "New Staff", uuid4()
    )
    assert user == repo.invite.return_value
    sent_url = delivery.invites["invited@example.test"]
    token = parse_qs(urlsplit(sent_url).fragment)["token"][0]
    assert repo.invite.call_args.kwargs["digest"] == hashlib.sha256(token.encode()).digest()
    await auth.accept_invitation(token, PASSWORD)
    repo.complete_invite.assert_awaited_once()
    delivery.fail_invite = True
    with pytest.raises(HTTPException) as exc:
        await auth.create_invitation(SimpleNamespace(), "other@example.test", "New Staff", uuid4())
    assert exc.value.status_code == 503
    repo.complete_invite.return_value = None
    with pytest.raises(HTTPException) as exc:
        await auth.accept_invitation("expired-token", PASSWORD)
    assert exc.value.status_code == 400
