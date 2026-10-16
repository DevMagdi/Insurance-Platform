"""Owner-scoped scheduled reports; the worker reauthorizes every run."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, require_permission
from app.api.staff import access_error
from app.domain.schemas import ReportScheduleSpec, ReportScheduleView
from app.repositories.reports import ReportScheduleRepository
from app.security.tokens import csrf_required
from app.services.reports import ReportService

router = APIRouter(prefix="/api/staff/reports", tags=["reports"])
LOCKED = ("reports.manage", "analytics.view", "analytics.export")


def service() -> ReportService:
    return ReportService(ReportScheduleRepository())


@router.get("/schedules")
async def schedules(
    ctx: RequestContext = Depends(require_permission("reports.manage")),
) -> list[ReportScheduleView]:
    return await service().schedules(ctx.db, ctx.scope)


@router.post("/schedules", status_code=201)
async def create_schedule(
    item: ReportScheduleSpec,
    ctx: RequestContext = Depends(require_permission("reports.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    for code in LOCKED:
        require_permission(code).verify(ctx)
    try:
        schedule_id = await service().save(ctx.db, None, item)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if schedule_id is None:
        raise HTTPException(status_code=404, detail="Report schedule unavailable")
    return {"id": schedule_id}


@router.put("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: UUID,
    item: ReportScheduleSpec,
    ctx: RequestContext = Depends(require_permission("reports.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    for code in LOCKED:
        require_permission(code).verify(ctx)
    try:
        saved = await service().save(ctx.db, schedule_id, item)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if saved is None:
        raise HTTPException(status_code=404, detail="Report schedule unavailable")
    return {"id": saved}


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: UUID,
    ctx: RequestContext = Depends(require_permission("reports.manage")),
    _: None = Depends(csrf_required),
) -> None:
    try:
        removed = await service().delete(ctx.db, schedule_id)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Report schedule unavailable")


@router.post("/schedules/{schedule_id}/run", status_code=202)
async def run_schedule_now(
    schedule_id: UUID,
    ctx: RequestContext = Depends(require_permission("reports.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, str]:
    require_permission("analytics.export").verify(ctx)
    try:
        queued = await service().queue_now(ctx.db, schedule_id)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if not queued:
        raise HTTPException(status_code=404, detail="Active report schedule unavailable")
    return {"status": "queued"}


@router.get("/schedules/{schedule_id}/runs")
async def schedule_runs(
    schedule_id: UUID,
    page: int = 1,
    page_size: int = 20,
    ctx: RequestContext = Depends(require_permission("reports.manage")),
) -> list[dict[str, Any]]:
    if not 1 <= page <= 100 or not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="Invalid run page")
    return await service().runs(ctx.db, schedule_id, page, page_size)
