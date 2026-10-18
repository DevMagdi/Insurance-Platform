"""Phase 8 unit tests: edge hardening, rate limiting, lockout, CAPTCHA, keyring,
SSRF, observability, backpressure, privacy API wiring and static audits."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import httpx
import pytest
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.main import app, http_policy
from app.middleware.body_limit import BodySizeLimitMiddleware
from app.middleware.origin import OriginGuardMiddleware
from app.middleware.policy import HttpPolicy
from app.middleware.rate_limit import ENDPOINT_CLASSES, RateLimitMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.observability import metrics
from app.observability.logging import JsonFormatter, redact
from app.observability.sentry import before_send
from app.scripts.lint_migrations import lint_sql, linted_files
from app.security import ssrf
from app.security.captcha import CaptchaVerifier
from app.security.crypto import NationalIdProtector, build_keyring, idempotency_root
from app.security.integration_crypto import SecretCipher
from app.security.keyring import Keyring, parse_previous_keys
from app.security.lockout import AccountLocked, LoginLockout
from app.security.proxy import ClientIPResolver, is_private_address
from app.security.rate_limit import RateLimiter, RateLimitExceeded
from app.services.backpressure import DEPTH_KEY, QueueBackpressure
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
NID = "29901010112345"
# Disposable fixture material only. Production deploys must generate unique keys.
_TEST_JWT_PRIVATE_KEY_B64 = (
    "LS0tLS1CRUdJTiBQUklWQVRFIEtFWS0tLS0tCk1DNENBUUF3QlFZREsyVndCQ0lFSUdscW"
    "gxM3A2T0ttQ2tXd0tjR2t3dk1hL0tYWTFtRmFsMzR6dUhmdEJ0TWEKLS0tLS1FTkQgUFJJ"
    "VkFURSBLRVktLS0tLQo="
)
_TEST_JWT_PUBLIC_KEY_B64 = (
    "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUNvd0JRWURLMlZ3QXlFQW5pemIya3ZQT2"
    "84WnY2S2tqM3pQL1NQRzF6UlQ1T0hVUVVTTlBMQkYvbXM9Ci0tLS0tRU5EIFBVQkxJQyBL"
    "RVktLS0tLQo="
)


def key(byte: bytes) -> str:
    return base64.urlsafe_b64encode(byte * 32).decode()


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "DATABASE_URL": "postgresql+asyncpg://app_runtime@localhost/test",
        "APP_ENCRYPTION_KEY": key(b"e"),
        "APP_BLIND_INDEX_KEY": key(b"b"),
        "JWT_SIGNING_KEY": "s" * 64,
        "ENVIRONMENT": "test",
        **overrides,
    }
    return Settings(**values)


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


# --- Trusted proxies ---------------------------------------------------------


def test_client_ip_honours_only_trusted_proxy_chain() -> None:
    resolver = ClientIPResolver.from_csv("10.0.0.0/8,127.0.0.1/32")
    assert resolver.resolve("198.51.100.7", "1.2.3.4") == "198.51.100.7"  # untrusted peer
    assert resolver.resolve("10.0.0.5", "203.0.113.9, 10.0.0.9") == "203.0.113.9"
    assert resolver.resolve("10.0.0.5", "6.6.6.6, 203.0.113.9") == "203.0.113.9"  # spoof prefix
    assert resolver.resolve("10.0.0.5", "garbage, 10.1.1.1") == "10.0.0.5"
    assert resolver.resolve("10.0.0.5", "10.2.2.2") == "10.2.2.2"
    assert resolver.resolve(None, None) == "0.0.0.0"
    assert resolver.resolve("testclient", None) == "0.0.0.0"
    assert resolver.resolve("[2001:db8::1]:443", None) == "2001:db8::1"
    assert is_private_address("10.1.2.3") and not is_private_address("8.8.8.8")
    with pytest.raises(ValueError):
        ClientIPResolver.from_csv("not-a-cidr")


# --- Keyring / key rotation ----------------------------------------------------


def test_keyring_rotation_keeps_legacy_and_old_ciphertext_readable() -> None:
    old, new = b"o" * 32, b"n" * 32
    legacy = NationalIdProtector(old, b"b" * 32)
    legacy_blob = legacy.encrypt(NID)  # v2 under key id 1
    raw_legacy = os.urandom(12)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from app.security.crypto import _AAD

    pre_phase8 = raw_legacy + AESGCM(old).encrypt(raw_legacy, NID.encode(), _AAD)
    ring = Keyring(2, {1: old, 2: new})
    protector = NationalIdProtector(ring, b"b" * 32)
    assert protector.decrypt(legacy_blob) == NID
    assert protector.decrypt(pre_phase8) == NID
    assert ring.needs_rotation(legacy_blob) and ring.needs_rotation(pre_phase8)
    rotated = ring.rotate(pre_phase8, _AAD)
    assert rotated[:3] == b"KR\x02" and not ring.needs_rotation(rotated)
    assert NationalIdProtector(Keyring.single(new, 2), b"b" * 32).decrypt(rotated) == NID
    with pytest.raises(InvalidTag):
        NationalIdProtector(Keyring.single(b"x" * 32, 2), b"b" * 32).decrypt(rotated)
    with pytest.raises(ValueError):
        Keyring(3, {1: old})
    with pytest.raises(ValueError):
        Keyring(1, {1: b"short"})
    assert ring.key_ids() == (1, 2) and ring.contains_key(old)


def test_keyring_settings_and_stable_idempotency_root() -> None:
    assert parse_previous_keys(f"1:{key(b'o')}, 3:{key(b'p')}") == {1: key(b"o"), 3: key(b"p")}
    with pytest.raises(ValueError):
        parse_previous_keys("nokeyid")
    ring = build_keyring(key(b"n"), 2, f"1:{key(b'o')}")
    assert ring.primary_id == 2 and idempotency_root(ring) == b"o" * 32
    assert idempotency_root(ring, key(b"i")) == b"i" * 32
    with pytest.raises(ValueError):
        build_keyring(key(b"n"), 1, f"1:{key(b'o')}")
    rotated = settings(
        APP_ENCRYPTION_KEY=key(b"n"),
        APP_ENCRYPTION_KEY_ID=2,
        APP_ENCRYPTION_PREVIOUS_KEYS=f"1:{key(b'e')}",
    )
    assert rotated.idempotency_master_key() == b"e" * 32  # survives the rotation
    with pytest.raises(ValidationError):
        settings(APP_ENCRYPTION_PREVIOUS_KEYS=f"2:{key(b'b')}")  # blind key reused
    cipher = SecretCipher(rotated.encryption_keyring())
    blob = SecretCipher(b"e" * 32).encrypt(b"secret", associated_data="ctx")
    assert cipher.decrypt(cipher.rotate(blob, associated_data="ctx"), associated_data="ctx")


def test_phase8_settings_validation() -> None:
    assert settings().allowed_origins() == frozenset({"http://localhost:8080"})
    extra = settings(CORS_ALLOWED_ORIGINS="https://partner.example/, https://b.example")
    assert "https://partner.example" in extra.allowed_origins()
    for bad in (
        {"CORS_ALLOWED_ORIGINS": "ftp://x.example"},
        {"CORS_ALLOWED_ORIGINS": "https://x.example/path"},
        {"TRUSTED_PROXY_CIDRS": "nope"},
        {"TURNSTILE_SITE_KEY": "only-site"},
        {"METRICS_TOKEN": "short"},
        {"RATE_LIMIT_MULTIPLIER": "0"},
        {"DATABASE_READ_URL": "postgresql+asyncpg://postgres@replica/db"},
        {"MAX_JSON_BODY_BYTES": "10"},
        {"SENTRY_TRACES_SAMPLE_RATE": "2"},
    ):
        with pytest.raises(ValidationError):
            settings(**bad)
    production = {
        "ENVIRONMENT": "production",
        "TURNSTILE_SITE_KEY": "1x00000000000000000000AA",
        "TURNSTILE_SECRET_KEY": "1x0000000000000000000000000000000AA",
        "JWT_PRIVATE_KEY_B64": _TEST_JWT_PRIVATE_KEY_B64,
        "JWT_PUBLIC_KEY_B64": _TEST_JWT_PUBLIC_KEY_B64,
        "METRICS_TOKEN": "test-only-metrics-token-32-characters",
        "SMTP_HOST": "smtp.example.com",
        "SMTP_FROM": "notifications@example.com",
        "SMTP_START_TLS": True,
    }
    assert settings(**production).is_production
    with pytest.raises(ValidationError, match="METRICS_TOKEN"):
        settings(**{**production, "METRICS_TOKEN": None})
    with pytest.raises(ValidationError, match="JWT_PRIVATE_KEY_B64"):
        settings(**{**production, "JWT_PRIVATE_KEY_B64": None, "JWT_PUBLIC_KEY_B64": None})
    no_captcha = {
        name: value for name, value in production.items() if not name.startswith("TURNSTILE_")
    }
    with pytest.raises(ValidationError, match="Turnstile"):
        settings(**no_captcha)
    with pytest.raises(ValidationError):
        settings(**production, RATE_LIMIT_MULTIPLIER="5")
    with pytest.raises(ValidationError):
        settings(**production, CORS_ALLOWED_ORIGINS="http://insecure.example")
    policy = http_policy(settings(**production))
    assert policy.hsts_seconds and policy.production
    assert http_policy(settings()).hsts_seconds is None


# --- Sliding-window rate limiting and lockout --------------------------------


async def test_sliding_window_log_and_counter(redis: fakeredis.aioredis.FakeRedis) -> None:
    limiter = RateLimiter(redis, "k" * 32)
    decisions = [await limiter.hit("t", "user", 3, 60) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[0].headers()["X-RateLimit-Remaining"] == "2"
    assert int(decisions[3].headers()["Retry-After"]) >= 1
    with pytest.raises(RateLimitExceeded) as exc:
        await limiter.check("t", "user", 3, 60)
    assert exc.value.status_code == 429 and "Retry-After" in (exc.value.headers or {})
    big = [await limiter.hit("big", "ip", 250, 60) for _ in range(251)]
    assert all(d.allowed for d in big[:250]) and not big[250].allowed
    assert (await limiter.hit("t", "other-user", 3, 60)).allowed  # per-identity buckets
    relaxed = RateLimiter(redis, "k" * 32, multiplier=2)
    assert (await relaxed.hit("m", "u", 1, 60)).limit == 2


async def test_progressive_lockout(redis: fakeredis.aioredis.FakeRedis) -> None:
    lockout = LoginLockout(redis, "k" * 32)
    for _ in range(4):
        assert await lockout.failure("staff_login", "a@example.test") is None
    assert await lockout.failure("staff_login", "a@example.test") == 300
    with pytest.raises(AccountLocked) as locked:
        await lockout.ensure_allowed("staff_login", "a@example.test")
    assert locked.value.status_code == 429
    await lockout.ensure_allowed("staff_login", "b@example.test")
    await redis.flushdb()
    await lockout.failure("staff_totp", "x")
    await lockout.success("staff_totp", "x")
    for _ in range(4):
        assert await lockout.failure("staff_totp", "x") is None


async def test_auth_service_locks_staff_and_otp(redis: fakeredis.aioredis.FakeRedis) -> None:
    from contextlib import asynccontextmanager

    from app.services.auth import AuthService

    service = AuthService(SimpleNamespace(), redis, settings(), SimpleNamespace())  # type: ignore[arg-type]
    service.repo = SimpleNamespace(staff_by_email=AsyncMock(return_value=None))  # type: ignore[assignment]

    @asynccontextmanager
    async def fake_tx(*_args: object) -> AsyncIterator[object]:
        yield object()

    import app.services.auth as auth_module

    original = auth_module.purpose_transaction
    auth_module.purpose_transaction = fake_tx  # type: ignore[assignment]
    try:
        for _ in range(5):
            with pytest.raises(HTTPException) as failed:
                await service.staff_login("ghost@example.test", "bad-password", "1.2.3.4", "ua")
            assert failed.value.status_code == 401
        with pytest.raises(AccountLocked):
            await service.staff_login("ghost@example.test", "bad-password", "1.2.3.4", "ua")
    finally:
        auth_module.purpose_transaction = original


# --- CAPTCHA -------------------------------------------------------------------


async def test_turnstile_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    replies: list[Any] = []

    async def fake_post(self: httpx.AsyncClient, url: str, data: dict[str, str]) -> httpx.Response:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json=reply)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    verifier = CaptchaVerifier.from_settings("real-secret", "site", "https://app.example")
    assert verifier.public_config() == {"provider": "turnstile", "site_key": "site"}
    with pytest.raises(HTTPException) as missing:
        await verifier.verify(None, "otp_start", "1.2.3.4")
    assert missing.value.status_code == 400
    replies.append({"success": True, "action": "otp_start", "hostname": "app.example"})
    await verifier.verify("token", "otp_start", "1.2.3.4")
    replies.append({"success": True, "action": "staff_login", "hostname": "app.example"})
    with pytest.raises(HTTPException):
        await verifier.verify("token", "otp_start", "1.2.3.4")
    replies.append({"success": True, "action": "otp_start", "hostname": "evil.example"})
    with pytest.raises(HTTPException):
        await verifier.verify("token", "otp_start", "1.2.3.4")
    replies.append(httpx.ConnectError("down"))
    with pytest.raises(HTTPException) as down:
        await verifier.verify("token", "otp_start", "1.2.3.4")
    assert down.value.status_code == 503  # fail closed
    test_keys = CaptchaVerifier.from_settings("1x0000000000000000000000000000000AA", "s", "x")
    assert test_keys.expected_hostname is None
    await CaptchaVerifier().verify(None, "guest_track", "1.2.3.4")  # disabled


async def test_captcha_guard_rejects_public_form_without_token() -> None:
    saved = dict(app.state._state)
    app.state.captcha = CaptchaVerifier("secret", "site", None)
    app.state.auth = SimpleNamespace(limits=SimpleNamespace(check=AsyncMock()))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as client:
            response = await client.post(
                "/api/auth/customer/otp/start", json={"mobile": "+201123456789"}
            )
        assert response.status_code == 400 and "CAPTCHA" in response.text
    finally:
        app.state._state.clear()
        app.state._state.update(saved)


# --- Middleware stack ------------------------------------------------------------


def edge_app(policy: HttpPolicy | None = None, limiter: RateLimiter | None = None) -> FastAPI:
    test_app = FastAPI()
    test_app.state.http_policy = policy or HttpPolicy(
        allowed_origins=frozenset({"https://ok.example"})
    )
    if limiter is not None:
        test_app.state.rate_limiter = limiter

    @test_app.get("/api/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        if item_id == "missing":
            raise HTTPException(status_code=404)
        return {"id": item_id}

    @test_app.post("/api/items")
    async def create(payload: dict[str, Any]) -> dict[str, int]:
        return {"size": len(json.dumps(payload))}

    @test_app.post("/api/auth/staff/login")
    async def login() -> dict[str, str]:
        return {"ok": "yes"}

    @test_app.post("/api/webhooks/x/y")
    async def hook() -> dict[str, str]:
        return {"ok": "yes"}

    @test_app.get("/api/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    for middleware in (
        RateLimitMiddleware,
        BodySizeLimitMiddleware,
        OriginGuardMiddleware,
        SecurityHeadersMiddleware,
        RequestContextMiddleware,
    ):
        test_app.add_middleware(middleware)
    return test_app


async def test_security_headers_request_id_and_metrics() -> None:
    policy = HttpPolicy(hsts_seconds=600)
    async with AsyncClient(
        transport=ASGITransport(app=edge_app(policy)), base_url="https://t"
    ) as c:
        response = await c.get("/api/items/1", headers={"X-Request-ID": "abcdefgh-1234"})
        assert response.headers["X-Request-ID"] == "abcdefgh-1234"
        assert response.headers["Strict-Transport-Security"].startswith("max-age=600")
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "server" not in response.headers
        generated = await c.get("/api/items/2", headers={"X-Request-ID": "bad id!"})
        assert re.fullmatch(r"[0-9a-f]{32}", generated.headers["X-Request-ID"])
    sample = metrics.HTTP_REQUESTS.labels(method="GET", route="/api/items/{item_id}", status="200")
    assert sample._value.get() >= 2  # route template, never the raw path


async def test_origin_guard_and_cors_allow_list() -> None:
    async with AsyncClient(transport=ASGITransport(app=edge_app()), base_url="https://t") as c:
        assert (
            await c.post("/api/items", json={}, headers={"Origin": "https://evil.example"})
        ).status_code == 403
        assert (
            await c.post("/api/items", json={}, headers={"Origin": "https://t"})
        ).status_code == 200
        forwarded = await c.post(
            "/api/items",
            json={},
            headers={"Origin": "https://public.example", "X-Forwarded-Host": "public.example"},
        )
        assert forwarded.status_code == 200
        assert (
            await c.post("/api/items", json={}, headers={"Sec-Fetch-Site": "cross-site"})
        ).status_code == 403
        assert (
            await c.post("/api/webhooks/x/y", headers={"Origin": "https://evil.example"})
        ).status_code == 200
        ok = await c.post("/api/items", json={}, headers={"Origin": "https://ok.example"})
        assert ok.headers["Access-Control-Allow-Origin"] == "https://ok.example"
        assert ok.headers["Access-Control-Allow-Credentials"] == "true"
        preflight = await c.options(
            "/api/items",
            headers={"Origin": "https://ok.example", "Access-Control-Request-Method": "POST"},
        )
        assert (
            preflight.status_code == 204
            and "X-CSRF-Token" in preflight.headers["Access-Control-Allow-Headers"]
        )
        denied = await c.options(
            "/api/items",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
        assert denied.status_code == 403


async def test_body_limits_declared_and_streamed() -> None:
    policy = HttpPolicy(max_json_body=2048)
    async with AsyncClient(
        transport=ASGITransport(app=edge_app(policy)), base_url="https://t"
    ) as c:
        assert (await c.post("/api/items", json={"a": "b"})).status_code == 200
        assert (await c.post("/api/items", json={"a": "x" * 4096})).status_code == 413

        async def chunks() -> AsyncIterator[bytes]:
            yield b'{"a": "'
            for _ in range(10):
                yield b"x" * 512
            yield b'"}'

        streamed = await c.post(
            "/api/items", content=chunks(), headers={"Content-Type": "application/json"}
        )
        assert streamed.status_code == 413


async def test_edge_rate_limit_and_abuse_detection(redis: fakeredis.aioredis.FakeRedis) -> None:
    limiter = RateLimiter(redis, "k" * 32)
    policy = HttpPolicy(abuse_denied_threshold=5)
    test_app = edge_app(policy, limiter)
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="https://t") as c:
        codes = [(await c.post("/api/auth/staff/login")).status_code for _ in range(61)]
        assert codes[:60] == [200] * 60 and codes[60] == 429
        assert (await c.get("/api/health/live")).status_code == 200  # exempt
        await redis.flushdb()
        ok = await c.get("/api/items/1")
        assert ok.status_code == 200
        statuses = [(await c.get("/api/items/missing")).status_code for _ in range(12)]
        assert statuses[0] == 404 and statuses[-1] == 429
        blocked = await c.get("/api/items/1")
        assert blocked.status_code == 429 and "Retry-After" in blocked.headers
    assert metrics.SUSPICIOUS_ACCESS.labels(reason="denied_burst")._value.get() >= 1


def test_endpoint_classes_cover_sensitive_routes() -> None:
    def match(method: str, path: str) -> str | None:
        return next((c.name for c in ENDPOINT_CLASSES if c.matches(method, path)), None)

    cid = uuid4()
    assert match("POST", "/api/auth/customer/otp/start") == "auth"
    assert match("POST", "/api/customer/complaints") == "complaint_submit"
    assert match("PUT", f"/api/customer/attachments/uploads/{cid}/content") == "upload"
    assert match("GET", "/api/staff/complaints/export") == "export"
    assert match("GET", "/api/staff/audit/events.csv") == "export"
    assert match("POST", f"/api/staff/complaints/{cid}/ai/analyze") == "ai_call"
    assert match("POST", f"/api/staff/complaints/{cid}/voice-followups") == "ai_call"
    assert match("POST", f"/api/staff/complaints/{cid}/national-id:reveal") == "pii_reveal"
    assert match("GET", "/api/customer/privacy/export") == "privacy_export"
    assert match("GET", "/api/staff/storage") == "storage_inventory"
    assert match("GET", "/api/public/v1/complaints/CMP-1") == "public_api"
    assert match("GET", "/api/staff/complaints") is None


# --- Observability -----------------------------------------------------------------


def test_logs_and_sentry_events_are_redacted() -> None:
    text = "nid 29901010112345 mobile +201123456789 Bearer abcdefghijkl password=hunter2"
    clean = redact(text)
    assert "29901010112345" not in clean and "+201123456789" not in clean
    assert "hunter2" not in clean and "abcdefghijkl" not in clean
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "otp=123456", (), None)
    record.event = "test"
    line = json.loads(JsonFormatter("api", "1.0").format(record))
    assert line["message"] == "otp=[redacted]" and line["release"] == "1.0"
    event = {
        "request": {"cookies": {"a": "b"}, "headers": {"Authorization": "Bearer x"}, "data": "x"},
        "message": "national id 29901010112345",
    }
    scrubbed = before_send(event, {})
    assert scrubbed is not None and "29901010112345" not in json.dumps(scrubbed)
    assert "Bearer x" not in json.dumps(scrubbed)


async def test_metrics_endpoint_requires_token_and_production_docs_are_never_exposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = dict(app.state._state)
    monkeypatch.setenv("EXPOSE_API_DOCS", "true")  # there is no production override
    app.state.settings = SimpleNamespace(metrics_token=None, is_production=True)
    app.state.factory = None
    try:
        private_edge = ASGITransport(app=app, client=("10.2.3.4", 54321))
        async with AsyncClient(transport=private_edge, base_url="https://t") as c:
            assert (await c.get("/metrics")).status_code == 404
            app.state.settings = SimpleNamespace(metrics_token="m" * 32, is_production=True)
            assert (await c.get("/metrics")).status_code == 404
            ok = await c.get("/metrics", headers={"Authorization": f"Bearer {'m' * 32}"})
            assert ok.status_code == 200 and "http_requests_total" in ok.text
            paths = (
                "/docs",
                "/docs/",
                "/docs/oauth2-redirect",
                "/redoc",
                "/redoc/",
                "/openapi.json",
            )
            for path in paths:
                assert (await c.get(path)).status_code == 404
            config = await c.get("/api/public/config")
            assert config.json()["captcha"] is None
            assert config.headers["Cache-Control"] == "no-store"
            manifest = await c.get("/api/public/manifest.webmanifest")
            assert manifest.json()["name"] == "Insurance"
            assert manifest.json()["icons"][0]["src"] == "/insurance-logo.svg"
            assert manifest.headers["Cache-Control"] == "no-store"
    finally:
        app.state._state.clear()
        app.state._state.update(saved)


# --- SSRF -------------------------------------------------------------------------


async def test_ssrf_guard_blocks_private_and_rebinding(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: dict[str, list[str]] = {
        "public.example": ["93.184.215.14"],
        "metadata.example": ["169.254.169.254"],
        "mixed.example": ["93.184.215.14", "10.0.0.5"],
        "mapped.example": ["::ffff:127.0.0.1"],
    }

    async def fake(host: str, port: int) -> list[str]:
        if host not in answers:
            raise OSError("nxdomain")
        return answers[host]

    monkeypatch.setattr(ssrf, "resolve_host", fake)
    target = await ssrf.pin_public_url("https://public.example:8443/hook?x=1")
    assert target.url == "https://93.184.215.14:8443/hook?x=1"
    assert target.host_header == "public.example:8443"
    assert target.extensions == {"sni_hostname": "public.example"}
    for url in (
        "http://public.example/",
        "https://user:pw@public.example/",
        "https://metadata.example/",
        "https://mixed.example/",
        "https://mapped.example/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://unknown.example/",
        "https://bad:port/",
    ):
        with pytest.raises(ssrf.UnsafeDestination):
            await ssrf.pin_public_url(url)
    from app.services.integration_providers import (
        IntegrationProviderError,
        SignedWebhookProvider,
    )

    with pytest.raises(IntegrationProviderError, match="webhook_destination_blocked"):
        await SignedWebhookProvider().send("https://metadata.example/", "s", b"{}", timestamp=1)


# --- Backpressure -------------------------------------------------------------------


async def test_queue_backpressure(redis: fakeredis.aioredis.FakeRedis) -> None:
    control = QueueBackpressure(redis, threshold=10, retry_after=30)
    await control.ensure_capacity("integration")  # no telemetry → fail open
    await redis.hset(DEPTH_KEY, mapping={"integration": 11, "export": 2, "insurer": "x"})
    with pytest.raises(HTTPException) as busy:
        await control.ensure_capacity("integration")
    assert busy.value.status_code == 503 and busy.value.headers == {"Retry-After": "30"}
    await control.ensure_capacity("export")
    assert await control.depth("insurer") is None


async def test_worker_publishes_queue_depth_and_runs_retention(
    redis: fakeredis.aioredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import worker

    async def fake_query(engine: object, query: str, params: dict[str, Any]) -> list[Any]:
        if "queue_depths" in query:
            return [SimpleNamespace(queue="integration", depth=7)]
        assert params == {"sessions": 30, "outbox": 180}
        return [SimpleNamespace(purged={"auth_sessions": 3})]

    monkeypatch.setattr(worker, "_worker_query", fake_query)
    ctx: dict[str, Any] = {"engine": object(), "redis": redis}
    assert json.loads(await worker.publish_queue_metrics(ctx)) == {"integration": 7}
    assert await redis.hget(DEPTH_KEY, "integration") == b"7"
    assert json.loads(await worker.apply_retention(ctx)) == {"auth_sessions": 3}


# --- Static audits ---------------------------------------------------------------------


def test_no_dynamic_sql_outside_reviewed_allowlist() -> None:
    """SQLi regression guard: SQL text is static or built from constant whitelists."""
    allow = {"app/repositories/catalog.py", "app/scripts/rotate_keys.py"}
    pattern = re.compile(r"""(text|execute|exec_driver_sql)\(\s*f["']""")
    offenders = []
    for path in (ROOT / "backend/app").rglob("*.py"):
        relative = path.relative_to(ROOT / "backend").as_posix()
        if pattern.search(path.read_text(encoding="utf-8")) and relative not in allow:
            offenders.append(relative)
    assert offenders == []


def test_frontend_has_no_raw_html_sinks() -> None:
    sinks = re.compile(r"dangerouslySetInnerHTML|\.innerHTML\s*=|eval\(|new Function\(")
    hits = [
        str(path)
        for path in (ROOT / "frontend/src").rglob("*.ts*")
        if sinks.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_zero_downtime_migration_linter() -> None:
    assert [path.name for path in linted_files()][:2] == [
        "021_privacy_operations.sql",
        "022_rls_scope_initplans.sql",
    ]
    for path in linted_files():
        assert lint_sql(path.name, path.read_text(encoding="utf-8")) == []
    unsafe = """
        ALTER TABLE public.complaints ADD COLUMN flag boolean NOT NULL;
        ALTER TABLE public.complaints DROP COLUMN subject;
        CREATE INDEX ix_hot ON public.complaints (status);
        ALTER TABLE public.complaints ALTER COLUMN subject TYPE varchar(10);
        ALTER TABLE public.policies ADD CONSTRAINT fk_x FOREIGN KEY (customer_id)
            REFERENCES public.customers(id);
        CREATE TABLE public.fresh (id uuid);
        CREATE INDEX ix_fresh ON public.fresh (id);
    """
    messages = [finding.message for finding in lint_sql("x.sql", unsafe)]
    assert any("DEFAULT" in m for m in messages)
    assert any("DROP" in m for m in messages)
    assert any("CONCURRENTLY" in m and "complaints" in m for m in messages)
    assert any("TYPE" in m for m in messages)
    assert any("NOT VALID" in m for m in messages)
    assert not any("fresh" in m for m in messages)


def test_privacy_routes_are_documented_and_guarded() -> None:
    spec = app.openapi()
    consents = spec["paths"]["/api/customer/privacy/consents"]["post"]
    assert any(p["name"] == "X-CSRF-Token" for p in consents["parameters"])
    decide = spec["paths"]["/api/staff/privacy/requests/{request_id}"]["post"]
    assert decide["x-required-permission"] == "settings.manage"
    otp = spec["paths"]["/api/auth/customer/otp/start"]["post"]
    assert otp["x-captcha-action"] == "otp_start"
    assert any(p["name"] == "X-Captcha-Token" for p in otp["parameters"])
    assert spec["paths"]["/api/public/config"]["get"]["security"] == []
    assert "/metrics" not in spec["paths"]
