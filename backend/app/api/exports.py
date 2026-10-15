"""Async exports with execution-time and download-time DB reauthorization."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, require_permission, require_staff
from app.api.staff import access_error
from app.domain.schemas import ExportRequest
from app.repositories.exports import ExportRepository
from app.security.tokens import csrf_required
from app.services.storage import ObjectStorage

router = APIRouter(prefix="/api/staff/exports", tags=["exports"])


@router.post("", status_code=202)
async def request_export(
    item: ExportRequest,
    request: Request,
    ctx: RequestContext = Depends(require_staff),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    require_permission("complaints.view").verify(ctx)
    if item.kind == "complaints":
        require_permission("complaints.export").verify(ctx)
    else:
        require_permission("analytics.view").verify(ctx)
        require_permission("analytics.export").verify(ctx)
    await request.app.state.auth.limits.check("exports:create", str(ctx.actor.id), 5, 300)
    try:
        job_id = await ExportRepository().request(ctx.db, item)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    return {"id": job_id, "status": "queued"}


@router.get("")
async def export_jobs(ctx: RequestContext = Depends(require_staff)) -> list[dict[str, Any]]:
    return await ExportRepository().list(ctx.db, ctx.actor.id)


@router.get("/{job_id}/download")
async def download_export(
    job_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_staff),
) -> Response:
    await request.app.state.auth.limits.check("exports:download", str(ctx.actor.id), 15, 300)
    key = await ExportRepository().authorize_download(ctx.db, job_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Completed export unavailable")
    storage: ObjectStorage | None = request.app.state.storage
    if storage is None:
        raise HTTPException(status_code=503, detail="Private export storage unavailable")
    try:
        data = await storage.get(key, max_bytes=2_000_000)
    except Exception:
        raise HTTPException(status_code=503, detail="Private export storage unavailable") from None
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="insurance-export-{job_id}.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
