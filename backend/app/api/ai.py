"""Human-reviewed AI insights for scoped staff complaint work."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import RequestContext, require_permission
from app.domain.schemas import AiInsightGenerateRequest, AiInsightReviewRequest

router = APIRouter(prefix="/api/staff/complaints", tags=["ai-insights"])


@router.get("/{complaint_id}/ai-insights")
async def list_ai_insights(
    complaint_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> list[dict[str, Any]]:
    result = await request.app.state.ai.repository.list_for_complaint(
        ctx.db, ctx.scope, complaint_id
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Complaint not found")
    return cast(list[dict[str, Any]], result)


@router.post("/{complaint_id}/ai-insights/generate", status_code=201)
async def generate_ai_insights(
    complaint_id: UUID,
    item: AiInsightGenerateRequest,
    request: Request,
    ctx: RequestContext = Depends(require_permission("complaints.edit")),
) -> list[dict[str, Any]]:
    service = request.app.state.ai
    complaint = await service.repository.complaints.get(ctx.db, ctx.scope, complaint_id)
    if complaint is None:
        raise HTTPException(status_code=404, detail="Complaint not found")
    generated = service.analyzer.analyze(complaint, item.kinds)
    result = await service.repository.create(
        ctx.db, ctx.scope, complaint_id, generated, service.analyzer.model
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Complaint not found")
    return cast(list[dict[str, Any]], result)


@router.patch("/{complaint_id}/ai-insights/{insight_id}")
async def review_ai_insight(
    complaint_id: UUID,
    insight_id: UUID,
    item: AiInsightReviewRequest,
    request: Request,
    ctx: RequestContext = Depends(require_permission("complaints.edit")),
) -> dict[str, Any]:
    result = await request.app.state.ai.repository.review(
        ctx.db,
        ctx.scope,
        complaint_id,
        insight_id,
        item.review_status,
        ctx.actor.id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="AI insight not found")
    return cast(dict[str, Any], result)
