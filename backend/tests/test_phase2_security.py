"""Phase 2 authentication primitives, strict schemas, SQL scope and CSRF."""

from __future__ import annotations

import base64
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import fakeredis.aioredis
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api.deps import PermissionGate, RequestContext, client_ip, require_permission
from app.api.websocket import same_origin
from app.db import Actor, make_worker_engine
from app.domain.access import AccessScope
from app.domain.enums import InsuranceType, OwnershipLevel
from app.domain.schemas import (
    AccessSpec,
    ComplaintCreate,
    GuestTrackRequest,
    UploadFilePlan,
    UploadPlan,
)
from app.models.tickets import Attachment, AuditLog, Complaint
from app.security.passwords import StaffPasswords, TotpProtector
from app.security.rate_limit import MobileOTP, RateLimiter
from app.security.tokens import TokenManager, clear_auth_cookies, csrf_required, set_auth_cookies
from app.settings import Settings

VALID_NID = "29901010112345"  # Valid date/governorate format; synthetic test value.


@pytest.fixture
def settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="x" * 64,
        ENVIRONMENT="test",
    )


@pytest.fixture
async def redis() -> fakeredis.aioredis.FakeRedis:
    instance = fakeredis.aioredis.FakeRedis()
    yield instance
    await instance.aclose()


def create_payload() -> dict[str, object]:
    return {
        "national_id": VALID_NID,
        "mobile": "+201123456789",
        "company_id": str(uuid4()),
        "insurance_type": "health",
        "policy_number": "POL-000123",
        "subject": "Claim processing delay",
        "description": "The payment has not arrived after the expected date.",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("national_id", "39902290112345"),
        ("national_id", "٢٩٩٠١٠١٠١١٢٣٤٥"),
        ("mobile", "01123456789"),
        ("policy_number", "=HYPERLINK()"),
        ("company_id", "wrong"),
        ("insurance_type", "life"),
        # Revision 036 lowered the floor so a real one-line request such as
        # "محتاج موافقة أشعة" is accepted, but a description still cannot be
        # a couple of stray characters.
        ("description", "ab"),
        ("subject", "a"),
    ],
)
def test_complaint_strict_validation(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ComplaintCreate.model_validate({**create_payload(), field: value})
    with pytest.raises(ValidationError):
        ComplaintCreate.model_validate({**create_payload(), "extra": "smuggle"})


def test_guest_and_upload_validation() -> None:
    assert GuestTrackRequest(national_id=VALID_NID, ticket_no="CMP-2026-000001")
    with pytest.raises(ValidationError):
        GuestTrackRequest(national_id=VALID_NID, ticket_no="CMP-2026-1")
    for filename in ("../secret.pdf", "folder\\file.png", "unsafe\nfile.pdf"):
        with pytest.raises(ValidationError):
            UploadFilePlan(
                filename=filename,
                mime_type="application/pdf",
                size_bytes=100,
                checksum_sha256="a" * 64,
            )
    with pytest.raises(ValidationError):
        UploadPlan(
            files=[
                {
                    "filename": f"{i}.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 8_000_000,
                    "checksum_sha256": "a" * 64,
                }
                for i in range(4)
            ]
        )


def test_access_spec_rejects_invalid_modes_and_permission_overlap() -> None:
    company = uuid4()
    spec = AccessSpec(ownership="own", company_ids=[company], allow=["complaints.view"])
    assert spec.all_companies is False
    for changes in (
        {"company_ids": [], "all_companies": False},
        {"company_ids": [company], "all_companies": True},
        {"allow": ["complaints.view"], "deny": ["complaints.view"]},
        {"allow": ["not.a.permission"]},
    ):
        with pytest.raises(ValidationError):
            AccessSpec.model_validate({**spec.model_dump(), **changes})


def test_scope_compiles_to_conjunction_for_every_query() -> None:
    staff_id, company_a, company_b, team = uuid4(), uuid4(), uuid4(), uuid4()
    actor = Actor(staff_id, "staff")
    snapshot = {
        "id": str(staff_id),
        "all_companies": False,
        "company_ids": [str(company_a)],
        "insurance_types": ["health"],
        "ownership": "team",
        "team_ids": [str(team)],
    }
    scope = AccessScope.for_staff(actor, snapshot)
    compiled = str(
        select(Complaint.id).where(scope.predicate()).compile(dialect=postgresql.dialect())
    )
    assert "complaints.company_id IN" in compiled
    assert "complaints.insurance_type IN" in compiled
    assert "complaints.team_id IN" in compiled
    assert str(company_b) not in compiled
    own = AccessScope(actor, company_ids=frozenset({company_a}), ownership=OwnershipLevel.OWN)
    assert "complaints.assigned_agent_id" in str(own.predicate())
    assert "false" in str(AccessScope(actor).predicate()).lower()
    assert (
        "false"
        in str(
            AccessScope(actor, all_companies=True, ownership=OwnershipLevel.TEAM).predicate()
        ).lower()
    )
    customer = AccessScope.for_customer(Actor(staff_id, "customer"))
    assert "complaints.customer_id" in str(customer.predicate())
    with pytest.raises(ValueError):
        AccessScope.for_staff(actor, {**snapshot, "id": str(uuid4())})
    with pytest.raises(ValueError):
        AccessScope.for_customer(actor)
    global_scope = AccessScope(
        actor,
        all_companies=True,
        ownership=OwnershipLevel.ALL,
        insurance_types=frozenset({InsuranceType.HEALTH}),
    )
    assert "complaints.insurance_type" in str(global_scope.predicate())


def test_permission_gate_and_worker_role() -> None:
    gate: PermissionGate = require_permission("complaints.view")
    actor = Actor(uuid4(), "staff")
    ctx = RequestContext(
        SimpleNamespace(),
        actor,
        uuid4(),
        0,
        AccessScope(actor),
        frozenset(),
        None,
        SimpleNamespace(),
    )
    with pytest.raises(HTTPException) as exc:
        gate.verify(ctx)
    assert exc.value.status_code == 403
    ctx.permissions = frozenset({"complaints.view"})
    gate.verify(ctx)
    with pytest.raises(ValueError):
        require_permission("not.a.permission")
    with pytest.raises(ValueError):
        make_worker_engine("postgresql+asyncpg://app_runtime@localhost/test")


def test_tokens_expiration_type_integrity_and_cookie_security(settings: Settings) -> None:
    tokens = TokenManager(settings)
    actor = Actor(uuid4(), "staff")
    sid = uuid4()
    access = tokens.issue(actor, sid, 2, "access", timedelta(minutes=5))
    parsed = tokens.parse(access, "access")
    assert parsed.subject == actor and parsed.token_version == 2 and parsed.session_id == sid
    assert parsed.purpose is None
    assert jwt.get_unverified_header(access)["alg"] == "EdDSA"
    assert jwt.get_unverified_header(access)["kid"] == "active"

    # A new signer can retain the old public key by key ID during rotation.
    private_pem = tokens.keyring.private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = tokens.keyring.private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode()

    rotated_settings = settings.model_copy(
        update={
            "jwt_key_id": "next",
            "jwt_private_key_b64": encode(private_pem),
            "jwt_public_key_b64": encode(public_pem),
            "jwt_previous_public_keys": f"active:{encode(public_pem)}",
        }
    )
    rotated = TokenManager(rotated_settings)
    assert rotated.parse(access, "access").subject == actor
    next_token = rotated.issue(actor, sid, 2, "access", timedelta(minutes=5))
    assert jwt.get_unverified_header(next_token)["kid"] == "next"
    assert tokens.digest(access) != tokens.digest(access + "x")
    for payload, kind in [
        (access, "refresh"),
        (access + "x", "access"),
        (tokens.issue(actor, sid, 2, "access", timedelta(seconds=-1)), "access"),
    ]:
        with pytest.raises(HTTPException) as exc:
            tokens.parse(payload, kind)
        assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        tokens.parse(None, "access")
    response = Response()
    set_auth_cookies(response, access, access, settings)
    cookies = response.headers.getlist("set-cookie")
    assert any(
        "access_token=" in value and "HttpOnly" in value and "Secure" in value for value in cookies
    )
    csrf_cookie = next(
        value
        for value in cookies
        if "csrf_token=" in value and "Max-Age=0" not in value and "Path=/;" in value
    )
    assert "SameSite=lax" in csrf_cookie and "HttpOnly" not in csrf_cookie
    assert any(
        "csrf_token=" in value and "Max-Age=0" in value and "Path=/api" in value
        for value in cookies
    )
    clear_auth_cookies(response, settings)
    assert len(response.headers.getlist("set-cookie")) == 8
    cleared_csrf = sum(
        "csrf_token=" in value and "Max-Age=0" in value
        for value in response.headers.getlist("set-cookie")
    )
    assert cleared_csrf == 3


def test_csrf_and_websocket_origin() -> None:
    def request(cookie: str, header: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "headers": [
                    (b"cookie", f"csrf_token={cookie}".encode()),
                    (b"x-csrf-token", header.encode()),
                ],
                "client": ("127.0.0.1", 1234),
            }
        )

    csrf_required(request("correct", "correct"))
    with pytest.raises(HTTPException) as exc:
        csrf_required(request("correct", "wrong"))
    assert exc.value.status_code == 403
    assert client_ip(request("a", "a")) == "127.0.0.1"
    assert same_origin(
        SimpleNamespace(headers={"host": "example.test:443", "origin": "https://example.test:443"})
    )
    assert not same_origin(
        SimpleNamespace(headers={"host": "example.test:443", "origin": "https://evil.test"})
    )
    assert not same_origin(SimpleNamespace(headers={"host": "example.test:443"}))


async def test_redis_rate_limits_otp_attempts_and_single_use(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    limiter = RateLimiter(redis, "secret" * 12)
    for _ in range(2):
        await limiter.check("otp", "+201123456789", 2, 300)
    with pytest.raises(HTTPException) as exc:
        await limiter.check("otp", "+201123456789", 2, 300)
    assert exc.value.status_code == 429
    keys = await redis.keys("limit:*")
    assert keys and b"+201123456789" not in keys[0]
    otp = MobileOTP(redis, "secret" * 12, 300)
    code = await otp.start("+201123456789")
    with pytest.raises(HTTPException):
        await otp.start("+201123456789")
    with pytest.raises(HTTPException):
        await otp.verify("+201123456789", "999999" if code != "999999" else "888888")
    await otp.verify("+201123456789", code, consume=False)
    assert await redis.exists(otp._key("+201123456789"))
    await otp.verify("+201123456789", code)
    with pytest.raises(HTTPException):
        await otp.verify("+201123456789", code)
    other = "+201100000000"
    code = await otp.start(other)
    for _ in range(5):
        with pytest.raises(HTTPException):
            await otp.verify(other, "999999" if code != "999999" else "888888")
    with pytest.raises(HTTPException) as exc:
        await otp.verify(other, code)
    assert exc.value.status_code == 429


async def test_staff_password_and_totp_protection(settings: Settings) -> None:
    passwords = StaffPasswords()
    with pytest.raises(ValueError):
        passwords.validate("tooshort")
    for common in ("Password1234!", "P@ssw0rd2026", "Welcome1234!"):
        with pytest.raises(ValueError, match="commonly used"):
            passwords.validate(common)
    passwords.validate("Private-River-Cedar-48")
    hashed = await passwords.hash("long-password-123")
    assert hashed.startswith("$argon2id$")
    assert await passwords.verify(hashed, "long-password-123")
    assert not await passwords.verify(hashed, "wrong")
    assert not await passwords.verify(None, "wrong")
    protector = TotpProtector(settings)
    secret = protector.new_secret()
    blob = protector.encrypt(secret)
    assert protector.decrypt(blob) == secret
    assert blob != protector.encrypt(secret)
    import pyotp

    assert protector.verify(secret, pyotp.TOTP(secret).now())
    assert not protector.verify(secret, "000")
    assert "Insurance%20Platform" in protector.uri(secret, "person@example.test")
    with pytest.raises(ValueError):
        protector.decrypt(b"bad")


def test_insert_mappings_preserve_restricted_rls_reads_and_columns() -> None:
    # Customers cannot SELECT pending uploads and ordinary staff cannot SELECT
    # audit events. ORM inserts must never require those SELECT policies.
    for model in (Attachment, AuditLog):
        assert model.__mapper__.eager_defaults is False
        for column in model.__mapper__.primary_key:
            assert column.default is not None
            assert column.server_default is not None  # DB-side writers remain valid.
    # These complaint fields are not granted INSERT to app_runtime (even as NULL).
    for name in (
        "response_due_at",
        "sla_breached_at",
        "merged_into_id",
        "merged_into_created_at",
        "resolved_at",
        "csat_score",
    ):
        assert Complaint.__table__.c[name].server_default is not None
