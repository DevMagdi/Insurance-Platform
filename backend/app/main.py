"""API composition: RLS transactions, provider adapters and the hardened HTTP edge.

Middleware (outermost first): request context/metrics/access log → security
headers → CORS allow-list + Origin CSRF guard → body-size limits → edge rate
limiting & abuse detection → FastAPI routing. All of it is pure ASGI (no
BaseHTTPMiddleware) so streaming responses, SSE and WebSockets are unaffected.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.api import (
    admin_integrations,
    analytics,
    attachments,
    audit,
    auth,
    catalog,
    customer,
    exports,
    integration_webhooks,
    integrations,
    operations,
    platform,
    privacy,
    public_api,
    reports,
    staff,
    storage_admin,
    websocket,
    workflow,
)
from app.db import make_engine, purpose_transaction
from app.middleware.body_limit import BodySizeLimitMiddleware
from app.middleware.origin import OriginGuardMiddleware
from app.middleware.policy import HttpPolicy
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.observability.logging import configure_logging
from app.observability.sentry import init_sentry
from app.openapi_contract import decorate_spec
from app.repositories.attachments import AttachmentRepository
from app.repositories.complaints import ComplaintRepository
from app.security.blobs import BlobCipher
from app.security.captcha import CaptchaVerifier
from app.security.crypto import NationalIdProtector, decode_key
from app.security.integration_crypto import SecretCipher, derive_idempotency_key
from app.security.proxy import ClientIPResolver
from app.security.rate_limit import RateLimiter
from app.security.tokens import TokenManager
from app.services.attachments import AttachmentService
from app.services.auth import AuthService
from app.services.backpressure import QueueBackpressure
from app.services.complaints import ComplaintService
from app.services.integrations import IntegrationService
from app.services.providers import SMTPDelivery
from app.settings import Settings, get_settings


def http_policy(settings: Settings) -> HttpPolicy:
    return HttpPolicy(
        resolver=ClientIPResolver.from_csv(settings.trusted_proxy_cidrs),
        allowed_origins=settings.allowed_origins(),
        hsts_seconds=settings.hsts_seconds if settings.is_production else None,
        max_json_body=settings.max_json_body_bytes,
        max_upload_body=settings.upload_max_bytes + 64 * 1024,
        production=settings.is_production,
        metrics_token=settings.metrics_token,
        abuse_denied_threshold=settings.abuse_denied_threshold,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        json_logs=settings.log_json,
        service="api",
        release=settings.app_release,
    )
    init_sentry(
        settings.sentry_dsn,
        settings.environment,
        settings.app_release,
        settings.sentry_traces_sample_rate,
    )
    close = await install_state(app, settings)
    try:
        yield
    finally:
        app.state.draining = True
        await close()


async def install_state(
    app: FastAPI, settings: Settings, *, redis: Redis | None = None
) -> Callable[[], Awaitable[None]]:
    """Wire every runtime dependency onto ``app.state``; returns the async closer.

    Shared by the lifespan and the integration/pen-test suites so tests exercise
    exactly the production composition (optionally with an injected Redis).
    """
    engine = make_engine(settings.database_url)
    owns_redis = redis is None
    redis = redis if redis is not None else Redis.from_url(settings.redis_url)
    app.state.settings = settings
    app.state.draining = False
    app.state.http_policy = http_policy(settings)
    app.state.engine = engine
    app.state.redis = redis
    app.state.factory = async_sessionmaker(engine, expire_on_commit=False)
    read_engine = make_engine(settings.database_read_url) if settings.database_read_url else None
    app.state.read_engine = read_engine
    app.state.read_factory = (
        async_sessionmaker(read_engine, expire_on_commit=False) if read_engine else None
    )
    app.state.rate_limiter = RateLimiter(
        redis, settings.jwt_signing_key, settings.rate_limit_multiplier
    )
    app.state.captcha = CaptchaVerifier.from_settings(
        settings.turnstile_secret_key, settings.turnstile_site_key, settings.public_app_url
    )
    app.state.backpressure = QueueBackpressure(redis, settings.queue_backpressure_threshold)

    async def platform_name() -> str:
        async with purpose_transaction(app.state.factory, "guest") as db:
            name = await db.scalar(
                text("SELECT platform_name FROM app_security.public_platform_brand()")
            )
        return str(name or "Insurance")

    async def platform_logo() -> Any:
        from app.services.brand_assets import detect_logo

        async with purpose_transaction(app.state.factory, "guest") as db:
            result = await db.execute(text("SELECT * FROM app_security.public_platform_logo()"))
            logo = result.one_or_none()
        if logo is None:
            return None
        return detect_logo(bytes(logo.logo_content), str(logo.logo_mime_type))

    delivery = SMTPDelivery(settings, brand_loader=platform_name, logo_loader=platform_logo)
    keyring = settings.encryption_keyring()
    # Private binaries (attachments, export artifacts, call recordings) are encrypted
    # with this keyring and stored in PostgreSQL; no object storage is involved.
    app.state.blobs = BlobCipher(keyring)
    integrations_service = IntegrationService(
        SecretCipher(keyring),
        derive_idempotency_key(settings.idempotency_master_key()),
        oauth_redirect_base=settings.public_app_url,
    )
    app.state.integrations = integrations_service

    async def send_staff_sms_otp(mobile: str, code: str) -> None:
        async with purpose_transaction(app.state.factory, "staff_auth") as db:
            await integrations_service.send_staff_otp(db, mobile, code)

    app.state.auth = AuthService(
        app.state.factory, redis, settings, delivery, staff_sms_sender=send_staff_sms_otp
    )
    protector = NationalIdProtector(keyring, decode_key(settings.app_blind_index_key))
    app.state.complaints = ComplaintService(
        ComplaintRepository(),
        protector,
        settings.jwt_signing_key,
        integration_events=integrations_service,
    )

    async def virus_scanner_configured(db: Any) -> bool:
        return bool(await db.scalar(text("SELECT app_security.virus_scanner_configured()")))

    app.state.attachments = AttachmentService(
        AttachmentRepository(),
        TokenManager(settings),
        app.state.blobs,
        scanner_configured=virus_scanner_configured,
    )

    async def close() -> None:
        if owns_redis:
            await redis.aclose()
        await engine.dispose()
        if read_engine is not None:
            await read_engine.dispose()

    return close


class ContractApp(FastAPI):
    """Keep /openapi.json and the checked-in schema aligned with runtime guards."""

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is None:
            self.openapi_schema = decorate_spec(self, super().openapi())
        return self.openapi_schema


app = ContractApp(
    title="Insurance Brokerage Complaint Management",
    version="0.8.0",
    lifespan=lifespan,
    # Served by app.api.platform so production can disable them at runtime.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
# add_middleware prepends, so the LAST one added is the outermost layer.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(OriginGuardMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestContextMiddleware)
app.include_router(auth.router)
app.include_router(customer.router)
app.include_router(staff.router)
app.include_router(workflow.router)
app.include_router(analytics.router)
app.include_router(audit.router)
app.include_router(catalog.router)
app.include_router(exports.router)
app.include_router(attachments.router)
app.include_router(integrations.router)
app.include_router(admin_integrations.router)
app.include_router(integration_webhooks.router)
app.include_router(public_api.router)
app.include_router(reports.router)
app.include_router(operations.router)
app.include_router(websocket.router)
app.include_router(privacy.router)
app.include_router(storage_admin.router)
app.include_router(platform.router)


logger = logging.getLogger(__name__)

# One client-safe answer for every dependency failure, mirroring /api/health/ready:
# callers must not learn WHICH dependency (database or cache) is down - that is
# operator knowledge, logged server-side instead. Without these handlers Starlette
# answers a bare text 500; a JSON 503 with Retry-After keeps the API contract and
# gives clients a graceful degradation path during outages.
DEPENDENCY_UNAVAILABLE = "Service temporarily unavailable; please retry shortly."

# Dependency failures do not always arrive wrapped: during pool checkout asyncpg
# lets the raw OSError escape (ConnectionRefusedError while PostgreSQL restarts)
# and asyncpg maps lost connections onto the builtin ConnectionError family, so
# the classifier accepts those alongside the ORM/client exception hierarchies.
DEPENDENCY_ERRORS = (SQLAlchemyError, RedisError, ConnectionError, TimeoutError)


@app.exception_handler(SQLAlchemyError)
async def database_failure(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.warning(
        "unhandled database error on %s %s", request.method, request.url.path, exc_info=exc
    )
    return JSONResponse(
        status_code=503,
        content={"detail": DEPENDENCY_UNAVAILABLE},
        headers={"Retry-After": "30"},
    )


@app.exception_handler(RedisError)
async def cache_failure(request: Request, exc: RedisError) -> JSONResponse:
    logger.warning("unhandled cache error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=503,
        content={"detail": DEPENDENCY_UNAVAILABLE},
        headers={"Retry-After": "30"},
    )


@app.exception_handler(Exception)
async def unhandled_failure(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort JSON answers for failures raised OUTSIDE the route layer.

    The pure-ASGI middleware stack (request context, rate limiting) touches the
    database and Redis before routing, so dependency failures can bypass the
    narrow handlers above; Starlette routes them here. Anything that is not a
    dependency failure becomes a generic JSON 500 - never a stack trace, never
    driver wording.
    """
    if isinstance(exc, DEPENDENCY_ERRORS):
        logger.warning(
            "dependency failure on %s %s", request.method, request.url.path, exc_info=exc
        )
        return JSONResponse(
            status_code=503,
            content={"detail": DEPENDENCY_UNAVAILABLE},
            headers={"Retry-After": "30"},
        )
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/api/health/live", tags=["health"])
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/health/ready", tags=["health"])
async def ready() -> dict[str, str]:
    if getattr(app.state, "draining", False):
        # Graceful shutdown: leave the load balancer before connections close.
        raise HTTPException(status_code=503, detail="Draining")
    engine: AsyncEngine = app.state.engine
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        redis: Redis = app.state.redis
        await redis.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Dependencies unavailable") from exc
    return {"status": "ready"}
