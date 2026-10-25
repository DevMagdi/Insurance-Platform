"""Unified CRM API under ``/api/crm`` — the platform's only CRM.

Leads with tenant-customisable pipeline stages (Kanban board + stage
management), scheduled tasks/follow-ups with due badges, one-click
Lead → corporate client conversion (Client Folder), salesperson
performance, Excel (.xlsx/.xls/.csv) import with dedup and a per-row
error ledger, and dashboard statistics. Migration 041 folded the former
B2B sales pipeline (``/api/staff/crm``) into these ``crm_*`` tables; that
router no longer exists.

Every route is behind the staff permission gates (``crm.view`` reads,
``crm.manage`` writes, ``crm.convert`` conversion) and every write
leaves an audit-log row. RLS confines the tables to the staff actor
scope; see migrations 040/041 and ``docs/CRM_MODULE.md``.
"""

from __future__ import annotations

import csv
import io
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, require_permission
from app.domain.crm_module_schemas import (
    CrmActivityCreate,
    CrmBulkAssign,
    CrmBulkStage,
    CrmLeadConvert,
    CrmLeadCreate,
    CrmLeadMove,
    CrmLeadUpdate,
    CrmStageCreate,
    CrmStageReorder,
    CrmStageUpdate,
    CrmTaskCreate,
    CrmTaskUpdate,
)
from app.models.crm_module import (
    CrmActivity,
    CrmImportBatch,
    CrmImportError,
    CrmLead,
    CrmPipelineStage,
    CrmTask,
)
from app.models.tickets import AuditLog
from app.repositories.crm_module import (
    CrmModuleRepository,
    activity_dict,
    batch_dict,
    lead_dict,
    stage_dict,
    task_dict,
)
from app.security.tokens import csrf_required
from app.services.crm_import import dedupe, map_rows, parse_leads_file, template_rows
from app.services.crm_pipeline import DEFAULT_STAGE_CODE, slugify_client_name
from app.services.render import tabular_bytes

router = APIRouter(prefix="/api/crm", tags=["crm"])

VIEW = Depends(require_permission("crm.view"))
MANAGE = Depends(require_permission("crm.manage"))
CONVERT = Depends(require_permission("crm.convert"))

repo = CrmModuleRepository()


def _lead_no() -> str:
    return f"LEAD-{datetime.now(UTC):%Y}-{secrets.token_hex(3).upper()}"


async def _audit(
    ctx: RequestContext,
    action: str,
    resource_id: UUID,
    after: dict[str, Any],
    resource_type: str = "crm_lead",
) -> None:
    async with ctx.db.begin_nested():
        ctx.db.add(
            AuditLog(
                actor_staff_id=ctx.actor.id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                after_state=after,
            )
        )
    await ctx.db.flush()


def _activity(
    ctx: RequestContext, lead_id: UUID, kind: str, description: str, task_id: UUID | None = None
) -> None:
    ctx.db.add(
        CrmActivity(
            id=uuid4(),
            tenant_id=repo.tenant_id,
            lead_id=lead_id,
            task_id=task_id,
            type=kind,
            description=description,
            created_by=ctx.actor.id,
        )
    )


def _is_salesperson(ctx: RequestContext) -> bool:
    """Sales role has CRM write access but no team-conversion permission.

    Treat that role as self-scoped at the API boundary; UI filters are never
    relied on for ownership isolation.
    """
    return (
        "crm.manage" in ctx.permissions
        and "crm.convert" not in ctx.permissions
        and not ctx.scope.all_companies
    )


async def _lead_or_404(ctx: RequestContext, lead_id: UUID) -> CrmLead:
    lead = await repo.lead(ctx.db, lead_id)
    if lead is None or (_is_salesperson(ctx) and lead.assigned_to != ctx.actor.id):
        # Do not disclose that a teammate's lead exists.
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


async def _task_or_404(ctx: RequestContext, task_id: UUID) -> CrmTask:
    task = await repo.task(ctx.db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if _is_salesperson(ctx):
        lead = await repo.lead(ctx.db, task.lead_id)
        if lead is None or lead.assigned_to != ctx.actor.id:
            raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _stage_or_422(ctx: RequestContext, code: str) -> CrmPipelineStage:
    stage = await repo.stage_by_code(ctx.db, code)
    if stage is None or not stage.is_active:
        raise HTTPException(status_code=422, detail="Pipeline stage is not active")
    return stage


def _conflict(exc: DBAPIError) -> HTTPException:
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate == "23505":
        return HTTPException(
            status_code=409, detail="A lead or stage with that identity already exists"
        )
    if sqlstate in {"23503", "23514", "22P02"}:
        return HTTPException(status_code=422, detail="CRM data is not valid")
    raise exc


def _apply_stage(
    ctx: RequestContext,
    lead: CrmLead,
    stage: CrmPipelineStage,
    lost_reason: str | None = None,
) -> None:
    """Move a lead, stamping/clearing ``closed_at`` from the stage flags."""
    previous = lead.stage_code
    lead.stage_code = stage.code
    if stage.is_won or stage.is_lost or stage.is_converted:
        lead.closed_at = lead.closed_at or datetime.now(UTC)
    else:
        lead.closed_at = None
    if stage.is_lost and lost_reason:
        lead.lost_reason = lost_reason
    lead.updated_at = datetime.now(UTC)
    if previous != stage.code:
        _activity(ctx, lead.id, "stage_change", f"Stage: {previous} → {stage.code}")


# --- Pipeline stages --------------------------------------------------------


@router.get("/stages")
async def list_stages(ctx: RequestContext = VIEW, include_inactive: bool = False) -> dict[str, Any]:
    stages = await repo.stages(ctx.db, include_inactive=include_inactive)
    result = []
    for stage in stages:
        result.append(stage_dict(stage, await repo.stage_lead_count(ctx.db, stage.code)))
    return {"stages": result}


@router.post("/stages", status_code=201)
async def create_stage(
    item: CrmStageCreate, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    stage = CrmPipelineStage(
        id=uuid4(),
        tenant_id=repo.tenant_id,
        code=item.code,
        name_ar=item.name_ar,
        name_en=item.name_en,
        sort_order=item.sort_order,
        is_won=item.is_won,
        is_lost=item.is_lost,
        is_converted=item.is_converted,
        is_active=item.is_active,
    )
    ctx.db.add(stage)
    try:
        await ctx.db.flush()
    except DBAPIError as exc:
        raise _conflict(exc) from exc
    await _audit(ctx, "crm_stage_created", stage.id, {"code": stage.code}, "crm_pipeline_stage")
    return stage_dict(stage, 0)


@router.post("/stages/reorder")
async def reorder_stages(
    item: CrmStageReorder, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    stages = await repo.stages(ctx.db, include_inactive=True)
    by_id = {stage.id: stage for stage in stages}
    if set(item.ids) != set(by_id) or len(item.ids) != len(by_id):
        raise HTTPException(status_code=422, detail="Reorder must list every stage exactly once")
    for position, stage_id in enumerate(item.ids, start=1):
        stage = by_id[stage_id]
        stage.sort_order = position * 10
        stage.updated_at = datetime.now(UTC)
    await ctx.db.flush()
    await _audit(
        ctx, "crm_stage_reordered", repo.tenant_id, {"count": len(item.ids)}, "crm_pipeline_stage"
    )
    return {"stages": [stage_dict(by_id[stage_id]) for stage_id in item.ids]}


@router.patch("/stages/{stage_id}")
async def update_stage(
    stage_id: UUID,
    item: CrmStageUpdate,
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    stage = await repo.stage(ctx.db, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail="Stage not found")
    data = item.model_dump(exclude_unset=True)
    if data.get("is_active") is False and stage.code == DEFAULT_STAGE_CODE:
        raise HTTPException(status_code=422, detail="The intake stage cannot be deactivated")
    for key, value in data.items():
        setattr(stage, key, value)
    stage.updated_at = datetime.now(UTC)
    try:
        await ctx.db.flush()
    except DBAPIError as exc:
        raise _conflict(exc) from exc
    await _audit(
        ctx, "crm_stage_updated", stage.id, {"changed": sorted(data)}, "crm_pipeline_stage"
    )
    return stage_dict(stage, await repo.stage_lead_count(ctx.db, stage.code))


@router.delete("/stages/{stage_id}")
async def delete_stage(
    stage_id: UUID, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    """Delete an empty stage; stages holding leads are protected (409)."""
    stage = await repo.stage(ctx.db, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail="Stage not found")
    if stage.code == DEFAULT_STAGE_CODE:
        raise HTTPException(status_code=422, detail="The intake stage cannot be deleted")
    if await repo.stage_lead_count(ctx.db, stage.code):
        raise HTTPException(
            status_code=409, detail="Stage still has leads; move them or deactivate it"
        )
    await ctx.db.execute(delete(CrmPipelineStage).where(CrmPipelineStage.id == stage.id))
    await _audit(ctx, "crm_stage_deleted", stage.id, {"code": stage.code}, "crm_pipeline_stage")
    return {"deleted": True}


# --- Leads CRUD + bulk --------------------------------------------------------


@router.get("/leads")
async def list_leads(
    ctx: RequestContext = VIEW,
    search: str | None = Query(default=None, max_length=120),
    stage_code: str | None = None,
    source: str | None = None,
    priority: str | None = None,
    assigned_to: UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    due_before: datetime | None = None,
    sort: str = "created_at",
    order: str = "desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    return await repo.leads_page(
        ctx.db,
        only_me=ctx.actor.id if _is_salesperson(ctx) else None,
        search=search,
        stage_code=stage_code,
        source=source,
        priority=priority,
        assigned_to=assigned_to,
        date_from=date_from,
        date_to=date_to,
        due_before=due_before,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )


@router.post("/leads", status_code=201)
async def create_lead(
    item: CrmLeadCreate, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    stage = await _stage_or_422(ctx, item.stage_code)
    lead = CrmLead(
        id=uuid4(),
        tenant_id=repo.tenant_id,
        lead_no=_lead_no(),
        full_name=item.full_name,
        company_name=item.company_name,
        phone=item.phone,
        email=item.email,
        source=item.source.value,
        stage_code=stage.code,
        priority=item.priority.value,
        insurance_type=item.insurance_type.value,
        insurer_company_id=item.insurer_company_id,
        expected_premium=item.expected_premium,
        assigned_to=ctx.actor.id if _is_salesperson(ctx) else (item.assigned_to or ctx.actor.id),
        notes=item.notes,
        created_by=ctx.actor.id,
    )
    if stage.is_won or stage.is_lost or stage.is_converted:
        lead.closed_at = datetime.now(UTC)
    ctx.db.add(lead)
    try:
        await ctx.db.flush()
    except DBAPIError as exc:
        raise _conflict(exc) from exc
    _activity(ctx, lead.id, "note", "Lead created")
    if item.next_followup_at is not None:
        task = CrmTask(
            id=uuid4(),
            tenant_id=repo.tenant_id,
            lead_id=lead.id,
            type="task",
            title="Follow-up",
            status="pending",
            due_at=item.next_followup_at,
            assigned_to=lead.assigned_to,
            created_by=ctx.actor.id,
        )
        ctx.db.add(task)
        await ctx.db.flush()
        _activity(ctx, lead.id, "task", "Task scheduled: Follow-up", task.id)
        await repo.refresh_next_followup(ctx.db, lead)
    await _audit(
        ctx, "crm_lead_created", lead.id, {"source": lead.source, "stage_code": lead.stage_code}
    )
    return lead_dict(lead)


@router.get("/leads/{lead_id}")
async def get_lead(lead_id: UUID, ctx: RequestContext = VIEW) -> dict[str, Any]:
    lead = await _lead_or_404(ctx, lead_id)
    return {
        **lead_dict(lead),
        "activities": await repo.activities(ctx.db, lead_id),
        "tasks": await repo.lead_tasks(ctx.db, lead_id),
    }


@router.patch("/leads/{lead_id}")
async def update_lead(
    lead_id: UUID,
    item: CrmLeadUpdate,
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    lead = await _lead_or_404(ctx, lead_id)
    data = item.model_dump(exclude_unset=True)
    if _is_salesperson(ctx) and "assigned_to" in data and data["assigned_to"] != ctx.actor.id:
        raise HTTPException(status_code=403, detail="Salespeople cannot reassign leads")
    old_stage, old_assignee = lead.stage_code, lead.assigned_to
    stage = (
        await _stage_or_422(ctx, item.stage_code)
        if item.stage_code and item.stage_code != old_stage
        else None
    )
    for key, value in data.items():
        if key == "stage_code":
            continue
        setattr(lead, key, value.value if hasattr(value, "value") else value)
    if stage is not None and stage.code != old_stage:
        _apply_stage(ctx, lead, stage, item.lost_reason)
    lead.updated_at = datetime.now(UTC)
    if "assigned_to" in data and lead.assigned_to != old_assignee:
        _activity(ctx, lead.id, "assignment", f"Assigned to {lead.assigned_to or '—'}")
    try:
        await ctx.db.flush()
    except DBAPIError as exc:
        raise _conflict(exc) from exc
    await _audit(ctx, "crm_lead_updated", lead.id, {"changed": sorted(data)})
    return lead_dict(lead)


@router.post("/leads/{lead_id}/move")
async def move_lead(
    lead_id: UUID,
    item: CrmLeadMove,
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    """Kanban drop: move one lead to another stage."""
    lead = await _lead_or_404(ctx, lead_id)
    stage = await _stage_or_422(ctx, item.stage_code)
    previous = lead.stage_code
    _apply_stage(ctx, lead, stage, item.lost_reason)
    try:
        await ctx.db.flush()
    except DBAPIError as exc:
        raise _conflict(exc) from exc
    await _audit(ctx, "crm_lead_moved", lead.id, {"from": previous, "to": stage.code})
    return lead_dict(lead)


@router.delete("/leads/{lead_id}")
async def delete_lead(
    lead_id: UUID, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    lead = await _lead_or_404(ctx, lead_id)
    await ctx.db.execute(delete(CrmLead).where(CrmLead.id == lead.id))
    await _audit(
        ctx,
        "crm_lead_deleted",
        lead.id,
        {"full_name": lead.full_name, "stage_code": lead.stage_code},
    )
    return {"deleted": True}


@router.post("/leads/bulk-assign")
async def bulk_assign(
    item: CrmBulkAssign, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    if _is_salesperson(ctx):
        raise HTTPException(status_code=403, detail="Team reassignment requires manager access")
    leads = await repo.leads_by_ids(ctx.db, item.ids)
    for lead in leads:
        lead.assigned_to = item.assigned_to
        lead.updated_at = datetime.now(UTC)
        _activity(ctx, lead.id, "assignment", f"Bulk assigned to {item.assigned_to or '—'}")
        await _audit(
            ctx, "crm_lead_bulk_assigned", lead.id, {"assigned_to": str(item.assigned_to or "")}
        )
    await ctx.db.flush()
    return {"updated": len(leads)}


@router.post("/leads/bulk-stage")
async def bulk_stage(
    item: CrmBulkStage, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    stage = await _stage_or_422(ctx, item.stage_code)
    leads = await repo.leads_by_ids(ctx.db, item.ids)
    if _is_salesperson(ctx) and any(lead.assigned_to != ctx.actor.id for lead in leads):
        raise HTTPException(status_code=404, detail="Lead not found")
    for lead in leads:
        previous = lead.stage_code
        _apply_stage(ctx, lead, stage)
        await _audit(ctx, "crm_lead_bulk_stage", lead.id, {"from": previous, "to": stage.code})
    await ctx.db.flush()
    return {"updated": len(leads)}


@router.post("/leads/{lead_id}/activities", status_code=201)
async def add_activity(
    lead_id: UUID,
    item: CrmActivityCreate,
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    await _lead_or_404(ctx, lead_id)
    row = CrmActivity(
        id=uuid4(),
        tenant_id=repo.tenant_id,
        lead_id=lead_id,
        type=item.type.value,
        description=item.description,
        created_by=ctx.actor.id,
    )
    ctx.db.add(row)
    await ctx.db.flush()
    await _audit(ctx, "crm_lead_activity", lead_id, {"type": row.type})
    return activity_dict(row)


@router.post("/leads/{lead_id}/convert")
async def convert_lead(
    lead_id: UUID,
    item: CrmLeadConvert,
    ctx: RequestContext = CONVERT,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    """One-click conversion: creates the corporate client with all lead data.

    The definer function carries contact fields onto the client (its
    Client Folder) and stamps the lead converted; activities and tasks
    stay on the lead, which now links to the client.
    """
    lead = await _lead_or_404(ctx, lead_id)
    if lead.converted_corporate_client_id is not None:
        raise HTTPException(status_code=409, detail="Lead is already converted")
    display_name = lead.company_name or lead.full_name
    slug = item.slug or slugify_client_name(display_name, str(lead.id))
    try:
        client_id = await ctx.db.scalar(
            text("SELECT app_security.convert_lead_to_client(:lead, :slug, :ar, :en)"),
            {
                "lead": lead_id,
                "slug": slug,
                "ar": item.name_ar or display_name,
                "en": item.name_en or display_name,
            },
        )
    except DBAPIError as exc:
        raise _conflict(exc) from exc
    _activity(ctx, lead_id, "conversion", f"Converted to client {slug}")
    await ctx.db.flush()
    await _audit(
        ctx,
        "crm_lead_converted",
        lead_id,
        {"corporate_client_id": str(client_id), "slug": slug},
    )
    return {"lead_id": str(lead_id), "corporate_client_id": str(client_id), "slug": slug}


# --- Tasks & follow-ups --------------------------------------------------------


@router.get("/tasks")
async def list_tasks(
    ctx: RequestContext = VIEW,
    status: str | None = None,
    type: str | None = None,
    assigned_to: UUID | None = None,
    lead_id: UUID | None = None,
    due: str | None = None,
) -> dict[str, Any]:
    return {
        "tasks": await repo.tasks(
            ctx.db,
            status=status,
            type_=type,
            assigned_to=assigned_to,
            lead_assigned_to=ctx.actor.id if _is_salesperson(ctx) else None,
            lead_id=lead_id,
            due=due,
        ),
        "due_counts": await repo.due_task_counts(
            ctx.db, ctx.actor.id if _is_salesperson(ctx) else None
        ),
    }


@router.post("/leads/{lead_id}/tasks", status_code=201)
async def create_task(
    lead_id: UUID,
    item: CrmTaskCreate,
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    lead = await _lead_or_404(ctx, lead_id)
    task = CrmTask(
        id=uuid4(),
        tenant_id=repo.tenant_id,
        lead_id=lead_id,
        type=item.type.value,
        title=item.title,
        description=item.description,
        status="pending",
        due_at=item.due_at,
        assigned_to=item.assigned_to or lead.assigned_to or ctx.actor.id,
        created_by=ctx.actor.id,
    )
    ctx.db.add(task)
    await ctx.db.flush()
    _activity(ctx, lead_id, "task", f"Task scheduled: {task.title}", task.id)
    await repo.refresh_next_followup(ctx.db, lead)
    await _audit(ctx, "crm_task_created", task.id, {"lead_id": str(lead_id)}, "crm_task")
    return task_dict(task, lead)


@router.patch("/tasks/{task_id}")
async def update_task(
    task_id: UUID,
    item: CrmTaskUpdate,
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    task = await _task_or_404(ctx, task_id)
    data = item.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(task, key, value.value if hasattr(value, "value") else value)
    if item.status is not None:
        task.completed_at = datetime.now(UTC) if item.status.value == "done" else None
        if item.status.value == "done":
            _activity(ctx, task.lead_id, "task", f"Task done: {task.title}", task.id)
    task.updated_at = datetime.now(UTC)
    await ctx.db.flush()
    lead = await repo.lead(ctx.db, task.lead_id)
    if lead is not None:
        await repo.refresh_next_followup(ctx.db, lead)
    await _audit(ctx, "crm_task_updated", task.id, {"changed": sorted(data)}, "crm_task")
    return task_dict(task, lead)


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: UUID, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    task = await _task_or_404(ctx, task_id)
    task.status = "done"
    task.completed_at = datetime.now(UTC)
    task.updated_at = datetime.now(UTC)
    _activity(ctx, task.lead_id, "task", f"Task done: {task.title}", task.id)
    await ctx.db.flush()
    lead = await repo.lead(ctx.db, task.lead_id)
    if lead is not None:
        await repo.refresh_next_followup(ctx.db, lead)
    await _audit(ctx, "crm_task_completed", task.id, {}, "crm_task")
    return task_dict(task, lead)


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: UUID, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    task = await _task_or_404(ctx, task_id)
    lead_id = task.lead_id
    await ctx.db.execute(delete(CrmTask).where(CrmTask.id == task.id))
    lead = await repo.lead(ctx.db, lead_id)
    if lead is not None:
        await repo.refresh_next_followup(ctx.db, lead)
    await _audit(ctx, "crm_task_deleted", task_id, {"lead_id": str(lead_id)}, "crm_task")
    return {"deleted": True}


# --- Kanban + performance + dashboard ------------------------------------------


@router.get("/pipeline")
async def pipeline(
    ctx: RequestContext = VIEW,
    assigned_to: UUID | None = None,
    source: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    return await repo.pipeline_board(
        ctx.db,
        assigned_to=ctx.actor.id if _is_salesperson(ctx) else assigned_to,
        source=source,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/reports/sales-performance")
async def sales_performance(
    ctx: RequestContext = VIEW,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    source: str | None = None,
    stage_code: str | None = None,
) -> dict[str, Any]:
    return {
        "salespeople": await repo.sales_performance(
            ctx.db,
            ctx.scope,
            date_from=date_from,
            date_to=date_to,
            source=source,
            stage_code=stage_code,
        )
    }


@router.get("/stats")
async def stats(ctx: RequestContext = VIEW) -> dict[str, Any]:
    return await repo.stats(
        ctx.db, ctx.scope, only_me=ctx.actor.id if _is_salesperson(ctx) else None
    )


# --- Excel import ----------------------------------------------------------


async def _read_rows(file: UploadFile) -> list[dict[str, str]]:
    content = await file.read(5_242_881)
    try:
        return parse_leads_file(file.filename or "", content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/imports/preview")
async def preview_import(
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Parse only: detected columns, first rows and row-level validity counts."""
    rows = await _read_rows(file)
    candidates, errors = map_rows(rows)
    return {
        "file_name": file.filename,
        "columns": sorted(rows[0].keys()) if rows else [],
        "total_rows": len(rows),
        "valid_rows": len(candidates),
        "invalid_rows": len(errors),
        "preview": rows[:10],
        "errors": [
            {"row_number": error.row_number, "reason": error.reason} for error in errors[:20]
        ],
    }


@router.post("/imports", status_code=201)
async def import_leads(
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Import a workbook in one transaction; duplicates become per-row errors."""
    rows = await _read_rows(file)
    candidates, map_errors = map_rows(rows)
    existing_phones, existing_emails = await repo.existing_contacts(
        ctx.db,
        [candidate.phone for candidate in candidates if candidate.phone],
        [candidate.email for candidate in candidates if candidate.email],
    )
    accepted, dup_errors, duplicates = dedupe(candidates, existing_phones, existing_emails)
    all_errors = [*map_errors, *dup_errors]
    batch = CrmImportBatch(
        id=uuid4(),
        tenant_id=repo.tenant_id,
        file_name=(file.filename or "import")[:200],
        uploaded_by=ctx.actor.id,
        total_rows=len(rows),
        success_rows=len(accepted),
        failed_rows=len(all_errors),
        status="completed_with_errors" if all_errors else "completed",
    )
    ctx.db.add(batch)
    leads: list[CrmLead] = []
    for candidate in accepted:
        lead = CrmLead(
            id=uuid4(),
            tenant_id=repo.tenant_id,
            lead_no=_lead_no(),
            full_name=candidate.full_name,
            phone=candidate.phone,
            email=candidate.email,
            source=candidate.source,
            stage_code=DEFAULT_STAGE_CODE,
            notes=candidate.notes,
            import_batch_id=batch.id,
            created_by=ctx.actor.id,
        )
        ctx.db.add(lead)
        leads.append(lead)
    # Flush parents before children: the platform metadata is large and
    # cyclic, so unit-of-work table ordering alone is not reliable here.
    try:
        await ctx.db.flush()
    except DBAPIError as exc:
        raise _conflict(exc) from exc
    for lead in leads:
        _activity(ctx, lead.id, "note", f"Imported from {batch.file_name}")
    for error in all_errors:
        ctx.db.add(
            CrmImportError(
                id=uuid4(),
                tenant_id=repo.tenant_id,
                batch_id=batch.id,
                row_number=error.row_number,
                reason=error.reason,
                raw_data=error.raw,
            )
        )
    try:
        await ctx.db.flush()
    except DBAPIError as exc:
        raise _conflict(exc) from exc
    await _audit(
        ctx,
        "crm_import_completed",
        batch.id,
        {
            "file_name": batch.file_name,
            "success_rows": batch.success_rows,
            "failed_rows": batch.failed_rows,
            "duplicates": duplicates,
        },
        "crm_import_batch",
    )
    return {
        "batch_id": str(batch.id),
        "file_name": batch.file_name,
        "total_rows": batch.total_rows,
        "success_rows": batch.success_rows,
        "failed_rows": batch.failed_rows,
        "duplicates": duplicates,
        "status": batch.status,
        "errors": [
            {"row_number": error.row_number, "reason": error.reason} for error in all_errors[:100]
        ],
    }


@router.get("/imports/template.xlsx")
async def import_template(ctx: RequestContext = VIEW) -> Response:
    headers, rows = template_rows()
    return Response(
        content=tabular_bytes("xlsx", "leads", headers, rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="crm-leads-template.xlsx"'},
    )


@router.get("/imports")
async def list_imports(ctx: RequestContext = VIEW) -> dict[str, Any]:
    return {"imports": await repo.batches(ctx.db)}


@router.get("/imports/{batch_id}")
async def get_import(batch_id: UUID, ctx: RequestContext = VIEW) -> dict[str, Any]:
    batch = await repo.batch(ctx.db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found")
    return {**batch_dict(batch), "errors": await repo.batch_errors(ctx.db, batch_id)}


@router.get("/imports/{batch_id}/errors.csv")
async def import_errors_csv(batch_id: UUID, ctx: RequestContext = VIEW) -> Response:
    batch = await repo.batch(ctx.db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found")
    errors = await repo.batch_errors(ctx.db, batch_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["row_number", "reason", "full_name", "phone", "email"])
    for error in errors:
        raw = error["raw_data"] or {}
        writer.writerow(
            [
                error["row_number"],
                error["reason"],
                raw.get("full_name", ""),
                raw.get("phone", ""),
                raw.get("email", ""),
            ]
        )
    return Response(
        content=buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="crm-import-{batch_id}-errors.csv"'},
    )
