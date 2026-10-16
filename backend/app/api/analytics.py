"""Scope- and permission-bound analytics with a required UTC-aware date window."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import RequestContext, require_permission
from app.domain.schemas import ComplaintFilter
from app.repositories.analytics import AnalyticsRepository
from app.services.analytics import AnalyticsService

router = APIRouter(prefix="/api/staff/analytics", tags=["analytics"])


def analytics(request: Request) -> AnalyticsService:
    return AnalyticsService(AnalyticsRepository(), request.app.state.redis)


@router.get("/overview")
async def overview(
    request: Request,
    filters: ComplaintFilter = Query(),
    ctx: RequestContext = Depends(require_permission("analytics.view")),
    _: RequestContext = Depends(require_permission("complaints.view")),
) -> dict[str, Any]:
    return await analytics(request).overview(ctx.db, ctx.scope, filters, ctx.token_version)


@router.get("/rollup")
async def rollup(
    request: Request,
    filters: ComplaintFilter = Query(),
    ctx: RequestContext = Depends(require_permission("analytics.view")),
    _: RequestContext = Depends(require_permission("complaints.view")),
) -> dict[str, Any]:
    """Pre-aggregated series for whole UTC days; falls back to a live query."""
    result = await analytics(request).rollup(ctx.db, ctx.scope, filters, ctx.token_version)
    return result


@router.get("/scorecards")
async def scorecards(
    request: Request,
    filters: ComplaintFilter = Query(),
    ctx: RequestContext = Depends(require_permission("analytics.view")),
    _: RequestContext = Depends(require_permission("complaints.view")),
) -> list[dict[str, Any]]:
    result = await analytics(request).overview(ctx.db, ctx.scope, filters, ctx.token_version)
    return result["company_scorecards"]  # type: ignore[no-any-return]
