"""Phase 2 API composition; RLS transactions and provider adapters share a lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.api import (
    analytics,
    attachments,
    audit,
    auth,
    catalog,
    customer,
    exports,
    staff,
    websocket,
    workflow,
)
from app.db import make_engine
from app.openapi_contract import decorate_spec
from app.repositories.attachments import AttachmentRepository
from app.repositories.complaints import ComplaintRepository
from app.security.crypto import NationalIdProtector, decode_key
from app.security.tokens import TokenManager
from app.services.attachments import AttachmentService
from app.services.auth import AuthService
from app.services.complaints import ComplaintService
from app.services.providers import SMTPDelivery
from app.services.storage import S3ObjectStorage
from app.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url)
    app.state.engine = engine
    app.state.redis = redis
    app.state.factory = async_sessionmaker(engine, expire_on_commit=False)
    delivery = SMTPDelivery(settings)
    app.state.auth = AuthService(app.state.factory, redis, settings, delivery)
    protector = NationalIdProtector(
        decode_key(settings.app_encryption_key), decode_key(settings.app_blind_index_key)
    )
    app.state.complaints = ComplaintService(
        ComplaintRepository(), protector, settings.jwt_signing_key
    )
    storage = (
        S3ObjectStorage(settings) if settings.s3_endpoint_url or settings.s3_access_key_id else None
    )
    app.state.storage = storage
    app.state.attachments = AttachmentService(
        AttachmentRepository(), TokenManager(settings), storage
    )
    try:
        yield
    finally:
        await redis.aclose()
        await engine.dispose()


class ContractApp(FastAPI):
    """Keep /openapi.json and the checked-in schema aligned with runtime guards."""

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is None:
            self.openapi_schema = decorate_spec(self, super().openapi())
        return self.openapi_schema


app = ContractApp(
    title="Insurance Brokerage Complaint Management", version="0.2.0", lifespan=lifespan
)
app.include_router(auth.router)
app.include_router(customer.router)
app.include_router(staff.router)
app.include_router(workflow.router)
app.include_router(analytics.router)
app.include_router(audit.router)
app.include_router(catalog.router)
app.include_router(exports.router)
app.include_router(attachments.router)
app.include_router(websocket.router)


@app.get("/api/health/live", tags=["health"])
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/health/ready", tags=["health"])
async def ready() -> dict[str, str]:
    engine: AsyncEngine = app.state.engine
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        redis: Redis = app.state.redis
        await redis.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Dependencies unavailable") from exc
    return {"status": "ready"}
