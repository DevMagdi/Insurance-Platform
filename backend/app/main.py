"""API composition: RLS transactions, provider adapters and the hardened HTTP edge.

Middleware (outermost first): request context/metrics/access log → security
headers → CORS allow-list + Origin CSRF guard → body-size limits → edge rate
limiting & abuse detection → FastAPI routing. All of it is pure ASGI (no
BaseHTTPMiddleware) so streaming responses, SSE and WebSockets are unaffected.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from redis.asyncio import Redis
from sqlalchemy import text
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
    websocket,
    workflow,
)
from app.db import make_engine
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
from app.services.storage import S3ObjectStorage
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
    app.state.expose_docs = os.environ.get("EXPOSE_API_DOCS", "").lower() == "true"
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
    delivery = SMTPDelivery(settings)
    app.state.auth = AuthService(app.state.factory, redis, settings, delivery)
    keyring = settings.encryption_keyring()
    integrations_service = IntegrationService(
        SecretCipher(keyring), derive_idempotency_key(settings.idempotency_master_key())
    )
    app.state.integrations = integrations_service
    protector = NationalIdProtector(keyring, decode_key(settings.app_blind_index_key))
    app.state.complaints = ComplaintService(
        ComplaintRepository(),
        protector,
        settings.jwt_signing_key,
        integration_events=integrations_service,
    )
    storage = (
        S3ObjectStorage(settings) if settings.s3_endpoint_url or settings.s3_access_key_id else None
    )
    app.state.storage = storage
    app.state.attachments = AttachmentService(
        AttachmentRepository(), TokenManager(settings), storage
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
app.include_router(platform.router)


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
