"""Staff workspace: atomic assignments, escalations, merges and private views."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from app.api.deps import RequestContext, require_permission
from app.domain.schemas import (
    AssignmentRequest,
    BulkAction,
    EscalationRequest,
    MergeRequest,
    SavedViewCreate,
)
from app.repositories.workflow import StaffWorkflowRepository
from app.security.tokens import csrf_required
from app.services.workflow import StaffWorkflowService

router = APIRouter(prefix="/api/staff", tags=["staff-workflow"])


def service(ctx: RequestContext) -> StaffWorkflowService:
    return StaffWorkflowService(ctx.complaints, StaffWorkflowRepository())


@router.post("/complaints/bulk")
async def bulk_action(
    item: BulkAction,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    # Role-based permission must also match the chosen action, not only view.
    code = "complaints.assign" if item.action == "assign" else "complaints.change_status"
    require_permission(code).verify(ctx)
    return await service(ctx).bulk(ctx.db, ctx.scope, item)


@router.post("/complaints/{complaint_id}/assign")
async def assign(
    complaint_id: UUID,
    item: AssignmentRequest,
    ctx: RequestContext = Depends(require_permission("complaints.assign")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await service(ctx).assign(ctx.db, ctx.scope, complaint_id, item)


@router.post("/complaints/{complaint_id}/reassign")
async def reassign(
    complaint_id: UUID,
    item: AssignmentRequest,
    ctx: RequestContext = Depends(require_permission("complaints.reassign")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await service(ctx).assign(ctx.db, ctx.scope, complaint_id, item)


@router.post("/complaints/{complaint_id}/escalate")
async def escalate(
    complaint_id: UUID,
    item: EscalationRequest,
    ctx: RequestContext = Depends(require_permission("complaints.escalate")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    require_permission("integrations.send_email").verify(ctx)
    return await service(ctx).escalate(ctx.db, ctx.scope, complaint_id, item.reason)


@router.post("/complaints/{complaint_id}/merge")
async def merge(
    complaint_id: UUID,
    item: MergeRequest,
    ctx: RequestContext = Depends(require_permission("complaints.merge")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await service(ctx).merge(ctx.db, ctx.scope, complaint_id, item.target_id)


@router.get("/complaints/{complaint_id}/duplicates")
async def duplicates(
    complaint_id: UUID,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> list[dict[str, Any]]:
    return await service(ctx).duplicates(ctx.db, ctx.scope, complaint_id)


@router.get("/complaints/{complaint_id}/canned-replies")
async def canned_replies(
    complaint_id: UUID,
    ctx: RequestContext = Depends(require_permission("complaints.reply_public")),
) -> list[dict[str, Any]]:
    parent = await ctx.complaints.parent(ctx.db, ctx.scope, complaint_id)
    return await StaffWorkflowRepository().canned_replies(ctx.db, ctx.scope, parent)


@router.get("/views")
async def saved_views(
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> list[dict[str, Any]]:
    records = await StaffWorkflowRepository().saved_views(ctx.db, ctx.scope)
    return [{"id": row.id, "name": row.name, "criteria": row.criteria} for row in records]


@router.post("/views", status_code=201)
async def create_saved_view(
    item: SavedViewCreate,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    try:
        record = await StaffWorkflowRepository().create_view(ctx.db, ctx.scope, item)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Saved view name already exists") from exc
    return {"id": record.id, "name": record.name, "criteria": record.criteria}


@router.put("/views/{view_id}")
async def update_saved_view(
    view_id: UUID,
    item: SavedViewCreate,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    repo = StaffWorkflowRepository()
    record = await repo.saved_view(ctx.db, ctx.scope, view_id, lock=True)
    if record is None:
        raise HTTPException(status_code=404, detail="Saved view not found")
    try:
        await repo.update_view(ctx.db, record, item)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Saved view name already exists") from exc
    return {"id": record.id, "name": record.name, "criteria": record.criteria}


@router.delete("/views/{view_id}", status_code=204)
async def delete_saved_view(
    view_id: UUID,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
    _: None = Depends(csrf_required),
) -> None:
    if not await StaffWorkflowRepository().delete_view(ctx.db, ctx.scope, view_id):
        raise HTTPException(status_code=404, detail="Saved view not found")


@router.get("/views/{view_id}/complaints")
async def saved_view_complaints(
    view_id: UUID,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> dict[str, Any]:
    return await service(ctx).saved_view_results(ctx.db, ctx.scope, view_id)


@router.patch("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: UUID,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    if not await StaffWorkflowRepository().mark_read(ctx.db, ctx.scope, notification_id):
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"read": True}
