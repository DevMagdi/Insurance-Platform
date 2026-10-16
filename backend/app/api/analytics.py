"""Scope- and permission-bound analytics with a required UTC-aware date window."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import RequestContext, require_permission
from app.db import scoped_transaction
from app.domain.schemas import ComplaintFilter
from app.repositories.analytics import AnalyticsRepository
from app.services.analytics import AnalyticsService

router = APIRouter(prefix="/api/staff/analytics", tags=["analytics"])


def analytics(request: Request) -> AnalyticsService:
    return AnalyticsService(AnalyticsRepository(), request.app.state.redis)


async def analytics_db(
    request: Request,
    ctx: RequestContext = Depends(require_permission("analytics.view")),
) -> AsyncIterator[AsyncSession]:
    """Route read-only aggregates to the replica when DATABASE_READ_URL is set.

    The session, token version and permissions were already re-validated on the
    PRIMARY by ``require_permission`` (so revocations apply immediately); only the
    heavy aggregate queries run on the replica, under the same RLS actor.
    """
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "read_factory", None
    )
    if factory is None:
        yield ctx.db
        return
    async with scoped_transaction(factory, ctx.actor, read_only=True) as db:
        yield db


@router.get("/overview")
async def overview(
    request: Request,
    filters: ComplaintFilter = Query(),
    ctx: RequestContext = Depends(require_permission("analytics.view")),
    _: RequestContext = Depends(require_permission("complaints.view")),
    db: AsyncSession = Depends(analytics_db),
) -> dict[str, Any]:
    return await analytics(request).overview(db, ctx.scope, filters, ctx.token_version)


@router.get("/rollup")
async def rollup(
    request: Request,
    filters: ComplaintFilter = Query(),
    ctx: RequestContext = Depends(require_permission("analytics.view")),
    _: RequestContext = Depends(require_permission("complaints.view")),
    db: AsyncSession = Depends(analytics_db),
) -> dict[str, Any]:
    """Pre-aggregated series for whole UTC days; falls back to a live query."""
    result = await analytics(request).rollup(db, ctx.scope, filters, ctx.token_version)
    return result


@router.get("/scorecards")
async def scorecards(
    request: Request,
    filters: ComplaintFilter = Query(),
    ctx: RequestContext = Depends(require_permission("analytics.view")),
    _: RequestContext = Depends(require_permission("complaints.view")),
    db: AsyncSession = Depends(analytics_db),
) -> list[dict[str, Any]]:
    result = await analytics(request).overview(db, ctx.scope, filters, ctx.token_version)
    return result["company_scorecards"]  # type: ignore[no-any-return]
