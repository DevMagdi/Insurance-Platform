"""Customer registration / identification flow: e-mail + mobile OTP matrix.

This suite is the regression guard for the entry point of every new customer.
It covers the full identity matrix (new/existing e-mail x new/existing mobile),
input validation, every rate limit and lockout on the flow, and — most
importantly — delivery failure behaviour: an unconfigured or broken SMTP
provider must never surface a configuration error such as
``"Email delivery is not configured"`` to the person registering. It must fail
gracefully with one customer-safe, retryable message while the real cause is
logged server-side.
"""

from __future__ import annotations

import base64
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import fakeredis.aioredis
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.db import Actor
from app.domain.schemas import CustomerVerifyRequest, MobileOTPRequest
from app.main import app
from app.repositories.auth import AuthRepository
from app.security.rate_limit import MobileOTP
from app.services.auth import OTP_DELIVERY_FAILED, AuthService
from app.services.providers import ProviderUnavailable, SMTPDelivery
from app.settings import Settings

NID = "29901010112345"
NEW_MOBILE = "+201100000001"
EXISTING_MOBILE = "+201123456789"
NEW_EMAIL = "brand.new.customer@example.com"
EXISTING_EMAIL = "existing.customer@example.com"
IP = "203.0.113.7"


class RecordingDelivery:
    """Delivery double that can simulate every provider failure mode."""

    def __init__(self) -> None:
        self.codes: dict[str, str] = {}
        self.unavailable = False  # ProviderUnavailable (e.g. SMTP not configured)
        self.crash: Exception | None = None  # any other provider exception

    async def send_otp(self, email: str, code: str) -> None:
        if self.unavailable:
            raise ProviderUnavailable("A real SMTP provider and sender address are required")
        if self.crash is not None:
            raise self.crash
        self.codes[email] = code

    async def send_invitation(self, email: str, url: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def send_password_reset(self, email: str, url: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def send_notice(self, *args: object, **kwargs: object) -> bool:  # pragma: no cover
        return False


def make_settings(**overrides: object) -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="test",
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture
async def env(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AuthService, Mock, RecordingDelivery, fakeredis.aioredis.FakeRedis, Settings]:
    settings = make_settings()
    redis = fakeredis.aioredis.FakeRedis()
    delivery = RecordingDelivery()
    service = AuthService(SimpleNamespace(), redis, settings, delivery)
    repo = Mock(spec=AuthRepository)
    for name in (
        "customers_by_mobile",
        "customer_email_matches",
        "register_customer",
        "current_version",
        "create_session",
    ):
        setattr(repo, name, AsyncMock())
    repo.current_version.return_value = 0
    repo.customers_by_mobile.return_value = []
    repo.customer_email_matches.return_value = True
    repo.register_customer.return_value = uuid4()
    service.repo = repo

    db = SimpleNamespace()

    @asynccontextmanager
    async def fake_purpose(factory: object, purpose: str):
        yield db

    @asynccontextmanager
    async def fake_scoped(factory: object, actor: Actor):
        yield db

    monkeypatch.setattr("app.services.auth.purpose_transaction", fake_purpose)
    monkeypatch.setattr("app.services.auth.scoped_transaction", fake_scoped)
    yield service, repo, delivery, redis, settings
    await redis.aclose()


def verify_request(
    mobile: str, email: str, code: str, *, register: bool = False
) -> CustomerVerifyRequest:
    kwargs: dict[str, object] = {"mobile": mobile, "email": email, "code": code}
    if register:
        kwargs.update(national_id=NID, full_name="Brand New Customer", whatsapp_available=True)
    return CustomerVerifyRequest(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# identity matrix
# --------------------------------------------------------------------------- #
async def test_brand_new_email_and_mobile_registers_and_sends_otp(env: tuple) -> None:
    """Task case 1: brand new e-mail + brand new mobile must succeed end to end."""
    auth, repo, delivery, redis, _settings = env
    await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    # The OTP was actually handed to the delivery provider for the new address.
    assert NEW_EMAIL in delivery.codes
    code = delivery.codes[NEW_EMAIL]

    # First verify without identity details: the flow asks for them (409) and
    # must NOT consume the code, so the customer can continue registering.
    with pytest.raises(HTTPException) as exc:
        await auth.verify_customer(verify_request(NEW_MOBILE, NEW_EMAIL, code), IP, "Browser")
    assert exc.value.status_code == 409
    assert exc.value.detail == "Provide national ID and name to register"
    assert await redis.exists(auth.otp._key(f"{NEW_MOBILE}:{NEW_EMAIL}"))

    # Completing identity details registers the customer and issues a session.
    tokens = await auth.verify_customer(
        verify_request(NEW_MOBILE, NEW_EMAIL, code, register=True), IP, "Browser"
    )
    assert repo.register_customer.await_count == 1
    kwargs = repo.register_customer.call_args.kwargs
    assert kwargs["full_name"] == "Brand New Customer"
    assert kwargs["mobile"] == NEW_MOBILE
    assert kwargs["email"] == NEW_EMAIL
    assert kwargs["whatsapp"] is True
    assert kwargs["last4"] == NID[-4:]
    assert auth.tokens.parse(tokens.access, "access").subject.scope == "customer"
    # The single-use code is consumed exactly once, at completion.
    assert not await redis.exists(auth.otp._key(f"{NEW_MOBILE}:{NEW_EMAIL}"))


async def test_existing_email_with_new_mobile_still_sends_otp(env: tuple) -> None:
    """Task case 2: an e-mail that already exists must not block the OTP send.

    Identity is anchored on the national ID/mobile; the e-mail is a contact
    channel. The start step never queries existence, so the code is delivered
    identically — and the response is indistinguishable (no enumeration).
    """
    auth, repo, delivery, _redis, _settings = env
    await auth.start_customer_otp(NEW_MOBILE, EXISTING_EMAIL, IP)
    assert EXISTING_EMAIL in delivery.codes
    code = delivery.codes[EXISTING_EMAIL]
    repo.customers_by_mobile.return_value = []
    tokens = await auth.verify_customer(
        verify_request(NEW_MOBILE, EXISTING_EMAIL, code, register=True), IP, "Browser"
    )
    assert tokens.access
    assert repo.register_customer.call_args.kwargs["email"] == EXISTING_EMAIL


async def test_existing_email_conflict_fails_gracefully(env: tuple) -> None:
    """A uniqueness conflict during registration is a clean 409, never a 500."""
    auth, repo, delivery, _redis, _settings = env
    await auth.start_customer_otp(NEW_MOBILE, EXISTING_EMAIL, IP)
    code = delivery.codes[EXISTING_EMAIL]
    repo.customers_by_mobile.return_value = []
    repo.register_customer.side_effect = IntegrityError("stmt", {}, Exception("unique"))
    with pytest.raises(HTTPException) as exc:
        await auth.verify_customer(
            verify_request(NEW_MOBILE, EXISTING_EMAIL, code, register=True), IP, "Browser"
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "Identity cannot be registered"


async def test_new_email_with_existing_mobile_is_refused_uniformly(env: tuple) -> None:
    """Task case 3: existing mobile + different e-mail → anti-enumeration 401."""
    auth, repo, delivery, _redis, _settings = env
    await auth.start_customer_otp(EXISTING_MOBILE, NEW_EMAIL, IP)
    code = delivery.codes[NEW_EMAIL]
    repo.customers_by_mobile.return_value = [(uuid4(), 0)]
    repo.customer_email_matches.return_value = False
    with pytest.raises(HTTPException) as exc:
        await auth.verify_customer(
            verify_request(EXISTING_MOBILE, NEW_EMAIL, code, register=True), IP, "Browser"
        )
    # Identical wording to a wrong code: the endpoint never reveals that the
    # mobile belongs to somebody else.
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid or expired code"
    assert repo.register_customer.await_count == 0


async def test_existing_email_and_mobile_logs_in_without_registering(env: tuple) -> None:
    """Task case 4: both exist → identification/login path, no new customer."""
    auth, repo, delivery, _redis, _settings = env
    await auth.start_customer_otp(EXISTING_MOBILE, EXISTING_EMAIL, IP)
    code = delivery.codes[EXISTING_EMAIL]
    customer_id = uuid4()
    repo.customers_by_mobile.return_value = [(customer_id, 3)]
    repo.customer_email_matches.return_value = True
    repo.current_version.return_value = 3
    tokens = await auth.verify_customer(
        verify_request(EXISTING_MOBILE, EXISTING_EMAIL, code), IP, "Browser"
    )
    claims = auth.tokens.parse(tokens.access, "access")
    assert claims.subject.id == customer_id
    assert claims.subject.scope == "customer"
    assert repo.register_customer.await_count == 0


async def test_shared_mobile_requires_national_id_disambiguation(env: tuple) -> None:
    """Several family members on one mobile: the flow asks for the national ID."""
    auth, repo, delivery, _redis, _settings = env
    await auth.start_customer_otp(EXISTING_MOBILE, EXISTING_EMAIL, IP)
    code = delivery.codes[EXISTING_EMAIL]
    repo.customers_by_mobile.return_value = [(uuid4(), 0), (uuid4(), 0)]
    repo.customer_email_matches.return_value = True
    with pytest.raises(HTTPException) as exc:
        await auth.verify_customer(
            verify_request(EXISTING_MOBILE, EXISTING_EMAIL, code), IP, "Browser"
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "Provide national ID to identify the customer"


async def test_wrong_code_never_reveals_identity_state(env: tuple) -> None:
    auth, repo, delivery, _redis, _settings = env
    await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    real = delivery.codes[NEW_EMAIL]
    wrong = "000000" if real != "000000" else "000001"
    repo.customers_by_mobile.return_value = [(uuid4(), 0)]
    with pytest.raises(HTTPException) as exc:
        await auth.verify_customer(verify_request(NEW_MOBILE, NEW_EMAIL, wrong), IP, "Browser")
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid or expired code"


async def test_otp_code_replay_is_rejected(env: tuple) -> None:
    auth, repo, delivery, _redis, _settings = env
    await auth.start_customer_otp(EXISTING_MOBILE, EXISTING_EMAIL, IP)
    code = delivery.codes[EXISTING_EMAIL]
    repo.customers_by_mobile.return_value = [(uuid4(), 0)]
    await auth.verify_customer(verify_request(EXISTING_MOBILE, EXISTING_EMAIL, code), IP, "UA")
    with pytest.raises(HTTPException) as exc:
        await auth.verify_customer(verify_request(EXISTING_MOBILE, EXISTING_EMAIL, code), IP, "UA")
    assert exc.value.status_code == 401


async def test_email_case_and_whitespace_are_normalized(env: tuple) -> None:
    """`Mona@Example.COM ` and `mona@example.com` are the same identity, and
    the code is delivered to the normalized address."""
    auth, _repo, delivery, redis, _settings = env
    await auth.start_customer_otp(NEW_MOBILE, "  Mona@Example.COM ", IP)
    assert "mona@example.com" in delivery.codes
    assert await redis.exists(auth.otp._key(f"{NEW_MOBILE}:mona@example.com"))


# --------------------------------------------------------------------------- #
# validation (task: invalid e-mail / mobile formats)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "email",
    [
        "",
        "not-an-email",
        "missing@tld",
        "@no-local.com",
        "two@@signs.com",
        "spaces in@x.com",
        "x" * 250 + "@y.com",
    ],
)
def test_invalid_email_formats_are_rejected(email: str) -> None:
    with pytest.raises(ValidationError):
        MobileOTPRequest(mobile=NEW_MOBILE, email=email)


def test_degenerate_email_domains_are_rejected() -> None:
    """QA finding L-1 (fixed): the pattern was tightened to
    ``^[^@\\s]+@[^@\\s.]+(\\.[^@\\s.]+)+$`` so empty domain labels (``a@b..com``),
    bare TLDs and leading/trailing dots are rejected at the schema, while
    ordinary addresses - sub-domains, plus-tags, unicode - stay valid."""
    for bad in ("a@b..com", "a@.com", "a@b.com.", "a@b", "a@b...com"):
        with pytest.raises(ValidationError):
            MobileOTPRequest(mobile=NEW_MOBILE, email=bad)
    for good in ("a@b.co", "user+tag@sub.example.com", "منير@مثال.مصر"):
        assert MobileOTPRequest(mobile=NEW_MOBILE, email=good).email == good


@pytest.mark.parametrize(
    "mobile",
    [
        "",
        "01123456789",  # local format without +20 is rejected at the API
        "+20212345678",  # Cairo landline prefix
        "+2012345678",  # too short
        "+2011234567890",  # too long
        "+14155550123",  # US number: customers must be Egyptian mobiles
        "+2011abc56789",  # letters
        "٠١١٢٣٤٥٦٧٨٩",  # Arabic-Indic digits
        "+20 11 2345 6789",  # spaces
    ],
)
def test_invalid_mobile_formats_are_rejected(mobile: str) -> None:
    with pytest.raises(ValidationError):
        MobileOTPRequest(mobile=mobile, email=NEW_EMAIL)


@pytest.mark.parametrize(
    "mobile", ["+201023456789", "+201123456789", "+201223456789", "+201523456789"]
)
def test_valid_egyptian_mobile_prefixes_are_accepted(mobile: str) -> None:
    assert MobileOTPRequest(mobile=mobile, email=NEW_EMAIL).mobile == mobile


def test_verify_request_validates_code_and_national_id() -> None:
    with pytest.raises(ValidationError):
        CustomerVerifyRequest(mobile=NEW_MOBILE, email=NEW_EMAIL, code="12345")
    with pytest.raises(ValidationError):
        CustomerVerifyRequest(mobile=NEW_MOBILE, email=NEW_EMAIL, code="abcdef")
    with pytest.raises(ValidationError):
        CustomerVerifyRequest(mobile=NEW_MOBILE, email=NEW_EMAIL, code="123456", national_id="123")
    with pytest.raises(ValidationError):
        CustomerVerifyRequest(
            mobile=NEW_MOBILE, email=NEW_EMAIL, code="123456", national_id=NID, full_name="A"
        )
    # extra fields are forbidden (strict schema)
    with pytest.raises(ValidationError):
        CustomerVerifyRequest(
            mobile=NEW_MOBILE,
            email=NEW_EMAIL,
            code="123456",
            is_admin=True,  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# rate limits and lockouts on the registration flow
# --------------------------------------------------------------------------- #
async def test_mobile_rate_limit_three_per_ten_minutes(env: tuple) -> None:
    auth, _repo, _delivery, _redis, _settings = env
    # The otp:mobile budget is per mobile across e-mail addresses: 3 per 10 min.
    for index in range(3):
        await auth.start_customer_otp(NEW_MOBILE, f"limit{index}@example.com", IP)
    with pytest.raises(HTTPException) as exc:
        await auth.start_customer_otp(NEW_MOBILE, "limit3@example.com", IP)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"]


async def test_email_rate_limit_spans_different_mobiles(env: tuple) -> None:
    auth, _repo, _delivery, _redis, _settings = env
    for index in range(3):
        await auth.start_customer_otp(f"+2011000001{index:02d}", NEW_EMAIL, IP)
    with pytest.raises(HTTPException) as exc:
        await auth.start_customer_otp("+201100000199", NEW_EMAIL, IP)
    assert exc.value.status_code == 429


async def test_ip_rate_limit_caps_bulk_otp_abuse(env: tuple) -> None:
    auth, _repo, _delivery, _redis, _settings = env
    auth.limits.multiplier = 0.1  # 20 -> 2 per hour for this test only
    await auth.start_customer_otp("+201100000201", "a1@example.com", IP)
    await auth.start_customer_otp("+201100000202", "a2@example.com", IP)
    with pytest.raises(HTTPException) as exc:
        await auth.start_customer_otp("+201100000203", "a3@example.com", IP)
    assert exc.value.status_code == 429


async def test_outstanding_code_blocks_a_second_start(env: tuple) -> None:
    auth, _repo, _delivery, _redis, _settings = env
    await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    with pytest.raises(HTTPException) as exc:
        await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    assert exc.value.status_code == 429
    assert exc.value.detail == "Wait for the current code to expire"


async def test_five_wrong_verify_attempts_burn_the_code(env: tuple) -> None:
    auth, repo, delivery, redis, _settings = env
    await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    real = delivery.codes[NEW_EMAIL]
    wrong = "000000" if real != "000000" else "000001"
    repo.customers_by_mobile.return_value = [(uuid4(), 0)]
    for _ in range(5):
        with pytest.raises(HTTPException) as exc:
            await auth.verify_customer(verify_request(NEW_MOBILE, NEW_EMAIL, wrong), IP, "UA")
        assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        await auth.verify_customer(verify_request(NEW_MOBILE, NEW_EMAIL, wrong), IP, "UA")
    assert exc.value.status_code == 429
    assert exc.value.detail == "Too many OTP attempts"
    assert not await redis.exists(auth.otp._key(f"{NEW_MOBILE}:{NEW_EMAIL}"))
    # even the real code is dead now
    with pytest.raises(HTTPException):
        await auth.verify_customer(verify_request(NEW_MOBILE, NEW_EMAIL, real), IP, "UA")


async def test_progressive_lockout_after_repeated_verify_failures(env: tuple) -> None:
    """The otp_verify lockout (8 failures/hour) blocks even the real code."""
    auth, repo, delivery, _redis, _settings = env
    repo.customers_by_mobile.return_value = [(uuid4(), 0)]
    await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    real = delivery.codes[NEW_EMAIL]
    wrong = "000000" if real != "000000" else "000001"
    statuses: list[int] = []
    for _ in range(8):
        with pytest.raises(HTTPException) as exc:
            await auth.verify_customer(verify_request(NEW_MOBILE, NEW_EMAIL, wrong), IP, "UA")
        statuses.append(exc.value.status_code)
    assert statuses[:5] == [401] * 5  # wrong code
    assert set(statuses[5:]) == {429}  # code burned, then attempts exhausted
    # The 9th call is refused by the account lockout before the OTP is checked.
    with pytest.raises(HTTPException) as exc:
        await auth.verify_customer(verify_request(NEW_MOBILE, NEW_EMAIL, wrong), IP, "UA")
    assert exc.value.status_code == 429
    assert exc.value.detail == "Too many failed attempts; try again later"
    assert exc.value.headers["Retry-After"]
    # Even a freshly issued, correct code cannot pass while the lock is on.
    await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    with pytest.raises(HTTPException) as exc:
        await auth.verify_customer(
            verify_request(NEW_MOBILE, NEW_EMAIL, delivery.codes[NEW_EMAIL]), IP, "UA"
        )
    assert exc.value.status_code == 429
    assert exc.value.detail == "Too many failed attempts; try again later"


# --------------------------------------------------------------------------- #
# delivery failure behaviour — the critical regression
# --------------------------------------------------------------------------- #
async def test_unconfigured_email_provider_never_leaks_configuration(
    env: tuple, caplog: pytest.LogCaptureFixture
) -> None:
    """The reported bug: a missing SMTP config answered the registration flow
    with ``{"detail":"Email delivery is not configured"}`` (422).

    Required behaviour: one graceful, customer-safe, retryable 503 message for
    new *and* existing e-mail addresses, no configuration wording, the pending
    code cancelled so a retry works immediately, and the true cause logged
    server-side only.
    """
    auth, _repo, delivery, redis, _settings = env
    delivery.unavailable = True
    with (
        caplog.at_level(logging.WARNING, logger="app.services.auth"),
        pytest.raises(HTTPException) as exc,
    ):
        await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    assert exc.value.status_code == 503
    assert exc.value.detail == OTP_DELIVERY_FAILED
    for banned in ("not configured", "smtp", "provider", "configuration"):
        assert banned not in exc.value.detail.lower()
    assert exc.value.headers["Retry-After"] == "120"
    # operator-side diagnostics still exist
    assert any("provider unavailable" in record.message for record in caplog.records)
    # the pending code was cancelled: an immediate retry is not blocked by the
    # outstanding-code cooldown once delivery recovers
    assert not await redis.exists(auth.otp._key(f"{NEW_MOBILE}:{NEW_EMAIL}"))
    delivery.unavailable = False
    await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    assert NEW_EMAIL in delivery.codes


async def test_provider_crash_uses_the_same_graceful_message(env: tuple) -> None:
    """A provider that is *down* (not merely unconfigured) behaves identically,
    so the endpoint cannot be used to distinguish failure modes."""
    auth, _repo, delivery, _redis, _settings = env
    delivery.crash = ConnectionRefusedError("smtp relay refused")
    with pytest.raises(HTTPException) as exc:
        await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    assert exc.value.status_code == 503
    assert exc.value.detail == OTP_DELIVERY_FAILED
    assert "refused" not in exc.value.detail.lower()
    assert "ConnectionRefused" not in exc.value.detail
    delivery.crash = None
    await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)


async def test_failed_delivery_does_not_burn_rate_limits(env: tuple) -> None:
    """Retries after an outage must not exhaust the 3-per-10-minutes budget
    faster than successful sends would (the attempt counter is the same, but a
    failed attempt must not also leave a code that blocks the retry)."""
    auth, _repo, delivery, redis, _settings = env
    delivery.crash = OSError("network down")
    with pytest.raises(HTTPException):
        await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    delivery.crash = None
    await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    assert await redis.exists(auth.otp._key(f"{NEW_MOBILE}:{NEW_EMAIL}"))


@pytest.mark.parametrize(
    ("host", "sender", "start_tls", "use_tls"),
    [
        (None, "no-reply@broker.example", True, False),  # no host at all
        ("", "no-reply@broker.example", True, False),  # empty host
        ("localhost", "no-reply@broker.example", True, False),  # loopback name
        ("mailpit", "no-reply@broker.example", True, False),  # dev sink name
        ("mailhog", "no-reply@broker.example", True, False),
        ("smtp.broker.local", "no-reply@broker.example", True, False),
        ("mailtrap.io", "no-reply@broker.example", True, False),  # sandbox marker
        ("smtp-relay.example.com", "no-reply@broker.test", True, False),  # fake sender TLD
        ("smtp-relay.example.com", "no-reply@broker.example", False, False),  # no TLS
    ],
)
async def test_smtp_delivery_fails_closed(
    host: str | None, sender: str, start_tls: bool, use_tls: bool
) -> None:
    """SMTPDelivery must refuse sandboxes, local hosts and plaintext."""
    config = SimpleNamespace(
        smtp_host=host,
        smtp_port=587,
        smtp_from=sender,
        smtp_username=None,
        smtp_password=None,
        smtp_start_tls=start_tls,
        smtp_use_tls=use_tls,
    )
    delivery = SMTPDelivery(config)
    with pytest.raises(ProviderUnavailable):
        await delivery.send_otp("customer@example.com", "123456")


async def test_otp_code_is_never_logged(env: tuple, caplog: pytest.LogCaptureFixture) -> None:
    auth, _repo, delivery, _redis, _settings = env
    with caplog.at_level(logging.DEBUG):
        await auth.start_customer_otp(NEW_MOBILE, NEW_EMAIL, IP)
    code = delivery.codes[NEW_EMAIL]
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert code not in combined
    assert NEW_EMAIL not in combined


async def test_mobile_otp_is_single_use_and_hashed_at_rest(env: tuple) -> None:
    """Redis stores a salted digest, never the plaintext code."""
    _auth, _repo, _delivery, redis, settings = env
    otp = MobileOTP(redis, settings.jwt_signing_key, settings.otp_seconds)
    code = await otp.start("+201199999999:x@example.com")
    raw = await redis.get(otp._key("+201199999999:x@example.com"))
    assert raw is not None
    assert code.encode() not in raw
    await otp.verify("+201199999999:x@example.com", code)
    with pytest.raises(HTTPException):
        await otp.verify("+201199999999:x@example.com", code)


# --------------------------------------------------------------------------- #
# HTTP surface (the exact bug the customer saw was an HTTP response body)
# --------------------------------------------------------------------------- #
@pytest.fixture
async def http(env: tuple, monkeypatch: pytest.MonkeyPatch):
    auth, repo, delivery, redis, settings = env
    app.state.auth = auth
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client, auth, repo, delivery, redis, settings


async def test_http_otp_start_returns_202_and_delivery_happened(http: tuple) -> None:
    client, _auth, _repo, delivery, _redis, _settings = http
    response = await client.post(
        "/api/auth/customer/otp/start",
        json={"mobile": NEW_MOBILE, "email": NEW_EMAIL},
        headers={"X-Forwarded-For": IP},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["message"] == "A one-time code has been sent to your email"
    assert NEW_EMAIL in delivery.codes


async def test_http_otp_start_with_broken_provider_is_graceful(http: tuple) -> None:
    """Regression for {"detail":"Email delivery is not configured"} (was 422)."""
    client, _auth, _repo, delivery, _redis, _settings = http
    delivery.unavailable = True
    response = await client.post(
        "/api/auth/customer/otp/start",
        json={"mobile": NEW_MOBILE, "email": NEW_EMAIL},
        headers={"X-Forwarded-For": IP},
    )
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail == OTP_DELIVERY_FAILED
    assert "Email delivery is not configured" not in response.text
    assert "not configured" not in detail.lower()
    assert response.headers["Retry-After"] == "120"


async def test_http_otp_start_rejects_invalid_payloads(http: tuple) -> None:
    client, _auth, _repo, _delivery, _redis, _settings = http
    cases = [
        {"mobile": "01123456789", "email": NEW_EMAIL},  # not +201…
        {"mobile": NEW_MOBILE, "email": "not-an-email"},
        {"mobile": NEW_MOBILE},  # missing email
        {"mobile": NEW_MOBILE, "email": NEW_EMAIL, "extra": 1},  # strict schema
        {"mobile": "+14155550123", "email": NEW_EMAIL},  # non-Egyptian
    ]
    for payload in cases:
        response = await client.post(
            "/api/auth/customer/otp/start", json=payload, headers={"X-Forwarded-For": IP}
        )
        assert response.status_code == 422, (payload, response.text)


async def test_http_full_registration_roundtrip(http: tuple) -> None:
    client, _auth, repo, delivery, _redis, _settings = http
    started = await client.post(
        "/api/auth/customer/otp/start",
        json={"mobile": NEW_MOBILE, "email": NEW_EMAIL},
        headers={"X-Forwarded-For": IP},
    )
    assert started.status_code == 202
    code = delivery.codes[NEW_EMAIL]
    repo.customers_by_mobile.return_value = []
    verified = await client.post(
        "/api/auth/customer/otp/verify",
        json={
            "mobile": NEW_MOBILE,
            "email": NEW_EMAIL,
            "code": code,
            "national_id": NID,
            "full_name": "Brand New Customer",
            "whatsapp_available": True,
        },
        headers={"X-Forwarded-For": IP},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json() == {"authenticated_as": "customer"}
    cookies = verified.headers.get_list("set-cookie")
    assert any("access_token=" in c and "HttpOnly" in c for c in cookies)
    assert any("refresh_token=" in c and "Path=/api/auth" in c for c in cookies)
    assert repo.register_customer.await_count == 1
