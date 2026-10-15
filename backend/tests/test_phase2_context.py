"""DB token_version and own session are rechecked despite Redis access caching."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import pytest
from fastapi import HTTPException, Request

from app.api import deps
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.security.tokens import TokenManager
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
    tokens = TokenManager(settings)
    actor = Actor(uuid4(), "staff")
    sid = uuid4()
    record = SimpleNamespace(
        id=sid,
        subject_id=actor.id,
        subject_kind="staff",
        token_version=3,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        revoked_at=None,
        last_used_at=None,
    )
    auth_repo = SimpleNamespace(
        get_session=AsyncMock(return_value=record), current_version=AsyncMock(return_value=3)
    )
    access_repo = SimpleNamespace(
        snapshot=AsyncMock(
            return_value={
                "id": str(actor.id),
                "status": "active",
                "permissions": ["complaints.view"],
                "all_companies": True,
                "company_ids": [],
                "insurance_types": [],
                "ownership": OwnershipLevel.ALL.value,
                "team_ids": [],
            }
        )
    )
    monkeypatch.setattr(deps, "AuthRepository", lambda: auth_repo)
    monkeypatch.setattr(deps, "AccessRepository", lambda: access_repo)
    redis = fakeredis.aioredis.FakeRedis()
    service = SimpleNamespace()
    yield tokens, actor, sid, record, auth_repo, access_repo, redis, service
    await redis.aclose()


async def test_effective_access_versioned_cache_and_immediate_revocation(fixture: tuple) -> None:
    tokens, actor, sid, record, auth, access, redis, service = fixture
    claims = tokens.parse(tokens.issue(actor, sid, 3, "access", timedelta(minutes=5)), "access")
    ctx = await deps.load_context(SimpleNamespace(), claims, redis, service)
    assert ctx.scope.actor == actor
    assert ctx.permissions == frozenset({"complaints.view"})
    assert record.last_used_at is not None
    assert await deps.require_permission("complaints.view")(ctx)
    access.snapshot.return_value["permissions"] = []
    cached = await deps.load_context(SimpleNamespace(), claims, redis, service)
    assert cached.permissions == ctx.permissions
    assert access.snapshot.await_count == 1
    # Access changes increment DB version and revoke stored sessions atomically.
    auth.current_version.return_value = 4
    with pytest.raises(HTTPException) as exc:
        await deps.load_context(SimpleNamespace(), claims, redis, service)
    assert exc.value.status_code == 401
    record.token_version = 4
    new = tokens.parse(tokens.issue(actor, sid, 4, "access", timedelta(minutes=5)), "access")
    changed = await deps.load_context(SimpleNamespace(), new, redis, service)
    assert changed.permissions == frozenset()
    assert access.snapshot.await_count == 2
    with pytest.raises(HTTPException):
        deps.require_permission("complaints.view").verify(changed)
    record.revoked_at = datetime.now(UTC)
    with pytest.raises(HTTPException):
        await deps.load_context(SimpleNamespace(), new, redis, service)


async def test_customer_context_is_not_promoted_to_staff(fixture: tuple) -> None:
    tokens, _actor, sid, record, auth, access, redis, service = fixture
    customer = Actor(uuid4(), "customer")
    record.subject_id = customer.id
    record.subject_kind = "customer"
    record.token_version = 0
    auth.current_version.return_value = 0
    claims = tokens.parse(tokens.issue(customer, sid, 0, "access", timedelta(minutes=5)), "access")
    ctx = await deps.load_context(SimpleNamespace(), claims, redis, service)
    assert ctx.scope == AccessScope.for_customer(customer)
    assert ctx.permissions == frozenset()
    access.snapshot.assert_not_awaited()
    with pytest.raises(HTTPException):
        await deps.require_staff(ctx)


def test_ip_fallback_never_trusts_arbitrary_forwarded_header() -> None:
    req = Request(
        {
            "type": "http",
            "method": "GET",
            "headers": [(b"x-forwarded-for", b"8.8.8.8"), (b"user-agent", b"Browser\x00bad")],
            "client": ("unknown-host", 1),
        }
    )
    assert deps.client_ip(req) == "0.0.0.0"
    assert deps.user_agent(req) == "Browserbad"
