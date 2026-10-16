"""Request-scoped RLS transaction, effective access cache and permissions."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Actor, scoped_transaction
from app.domain.access import AccessScope
from app.domain.permissions import PERMISSIONS
from app.middleware.policy import policy_for
from app.observability.logging import bind_actor
from app.repositories.access import AccessRepository
from app.repositories.auth import AuthRepository
from app.security.tokens import TokenClaims, TokenManager
from app.services.complaints import ComplaintService
from app.services.integrations import IntegrationService


@dataclass
class RequestContext:
    db: AsyncSession
    actor: Actor
    session_id: UUID
    token_version: int
    scope: AccessScope
    permissions: frozenset[str]
    snapshot: dict[str, Any] | None
    complaints: ComplaintService


def integration_service(request: Request) -> IntegrationService:
    return cast(IntegrationService, request.app.state.integrations)


def client_ip(request: Request) -> str:
    """Real client IP: X-Forwarded-For is honoured only from TRUSTED_PROXY_CIDRS."""
    resolved = request.scope.get("state", {}).get("client_ip")
    if isinstance(resolved, str):
        return resolved
    policy = policy_for(request.scope)
    peer = request.client.host if request.client else None
    ip = policy.resolver.resolve(peer, request.headers.get("x-forwarded-for"))
    request.scope.setdefault("state", {})["client_ip"] = ip
    return ip


def user_agent(request: Request) -> str:
    return "".join(
        char for char in request.headers.get("user-agent", "")[:256] if char.isprintable()
    )


async def load_context(
    db: AsyncSession,
    claims: TokenClaims,
    redis: Redis,
    complaints: ComplaintService,
) -> RequestContext:
    """Re-read the session and DB token_version on EVERY request/message."""
    auth = AuthRepository()
    record = await auth.get_session(db, claims.session_id)
    version = await auth.current_version(db)
    if (
        record is None
        or record.revoked_at is not None
        or record.expires_at <= datetime.now(UTC)
        or version is None
        or version != claims.token_version
        or record.token_version != version
        or record.subject_id != claims.subject.id
        or record.subject_kind != claims.subject.scope
    ):
        raise HTTPException(status_code=401, detail="Session expired or access changed")
    if record.last_used_at is None or record.last_used_at < datetime.now(UTC).replace(
        minute=0, second=0, microsecond=0
    ):
        record.last_used_at = datetime.now(UTC)
    if claims.subject.scope == "customer":
        return RequestContext(
            db,
            claims.subject,
            record.id,
            version,
            AccessScope.for_customer(claims.subject),
            frozenset(),
            None,
            complaints,
        )
    cache_key = f"access:{claims.subject.id}:{version}"
    cached = await redis.get(cache_key)
    snapshot = json.loads(cached) if cached else await AccessRepository().snapshot(db)
    if snapshot is None or snapshot.get("status") != "active":
        raise HTTPException(status_code=401, detail="Staff access unavailable")
    if not cached:
        await redis.set(cache_key, json.dumps(snapshot), ex=300)
    return RequestContext(
        db,
        claims.subject,
        record.id,
        version,
        AccessScope.for_staff(claims.subject, snapshot),
        frozenset(snapshot.get("permissions", [])),
        snapshot,
        complaints,
    )


async def authenticated_context(request: Request) -> AsyncIterator[RequestContext]:
    manager: TokenManager = request.app.state.auth.tokens
    claims = manager.parse(request.cookies.get("access_token"), "access")
    factory: async_sessionmaker[AsyncSession] = request.app.state.factory
    request.state.actor_id = str(claims.subject.id)
    request.state.actor_kind = claims.subject.scope
    bind_actor(str(claims.subject.id), claims.subject.scope)
    async with scoped_transaction(factory, claims.subject) as db:
        redis: Redis = request.app.state.redis
        yield await load_context(db, claims, redis, request.app.state.complaints)


async def require_customer(ctx: RequestContext = Depends(authenticated_context)) -> RequestContext:
    if ctx.actor.scope != "customer":
        raise HTTPException(status_code=403, detail="Customer account required")
    return ctx


async def require_staff(ctx: RequestContext = Depends(authenticated_context)) -> RequestContext:
    if ctx.actor.scope != "staff":
        raise HTTPException(status_code=403, detail="Staff account required")
    return ctx


@dataclass(frozen=True)
class PermissionGate:
    code: str

    def verify(self, ctx: RequestContext) -> None:
        if ctx.actor.scope != "staff" or self.code not in ctx.permissions:
            raise HTTPException(status_code=403, detail="Permission denied")

    async def __call__(self, ctx: RequestContext = Depends(require_staff)) -> RequestContext:
        self.verify(ctx)
        return ctx


def require_permission(code: str) -> PermissionGate:
    """Mandatory FastAPI dependency; also re-used for each WebSocket message."""
    if code not in PERMISSIONS:
        raise ValueError(f"Unknown staff permission {code}")
    return PermissionGate(code)
