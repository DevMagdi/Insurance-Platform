"""Isolated CRM module API under ``/api/crm`` (``crm_*`` tables).

Person-level lead management: CRUD + bulk operations, activity timeline,
Excel (.xlsx/.xls/.csv) import with dedup, per-row error ledger and a
downloadable template/error report, plus dashboard statistics. Every route
is behind the staff permission gates (``crm.view`` reads, ``crm.manage``
writes) and every write leaves an audit-log row. RLS confines the tables to
the staff actor scope; see migration 040.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import delete
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, require_permission
from app.domain.crm_module_schemas import (
    CrmActivityCreate,
    CrmBulkAssign,
    CrmBulkStatus,
    CrmLeadCreate,
    CrmLeadUpdate,
)
from app.models.crm_module import CrmActivity, CrmImportBatch, CrmImportError, CrmLead
from app.models.tickets import AuditLog
from app.repositories.crm_module import (
    CrmModuleRepository,
    activity_dict,
    batch_dict,
    lead_dict,
)
from app.security.tokens import csrf_required
from app.services.crm_import import dedupe, map_rows, parse_leads_file, template_rows
from app.services.render import tabular_bytes

router = APIRouter(prefix="/api/crm", tags=["crm-module"])

VIEW = Depends(require_permission("crm.view"))
MANAGE = Depends(require_permission("crm.manage"))

repo = CrmModuleRepository()


async def _audit(ctx: RequestContext, action: str, lead_id: UUID, after: dict[str, Any]) -> None:
    async with ctx.db.begin_nested():
        ctx.db.add(
            AuditLog(
                actor_staff_id=ctx.actor.id,
                action=action,
                resource_type="crm_lead",
                resource_id=lead_id,
                after_state=after,
            )
        )
    await ctx.db.flush()


def _activity(ctx: RequestContext, lead_id: UUID, kind: str, description: str) -> None:
    ctx.db.add(
        CrmActivity(
            id=uuid4(),
            lead_id=lead_id,
            type=kind,
            description=description,
            created_by=ctx.actor.id,
        )
    )


async def _lead_or_404(ctx: RequestContext, lead_id: UUID) -> CrmLead:
    lead = await repo.lead(ctx.db, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


def _conflict(exc: DBAPIError) -> HTTPException:
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate == "23505":
        return HTTPException(
            status_code=409, detail="A lead with that phone or email already exists"
        )
    if sqlstate in {"23503", "23514", "22P02"}:
        return HTTPException(status_code=422, detail="Lead data is not valid")
    raise exc


# --- Leads CRUD + bulk -----------------------------------------------------


@router.get("/leads")
async def list_leads(
    ctx: RequestContext = VIEW,
    search: str | None = Query(default=None, max_length=120),
    status: str | None = None,
    source: str | None = None,
    assigned_to: UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    sort: str = "created_at",
    order: str = "desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    return await repo.leads_page(
        ctx.db,
        search=search,
        status=status,
        source=source,
        assigned_to=assigned_to,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )


@router.post("/leads", status_code=201)
async def create_lead(
    item: CrmLeadCreate, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    lead = CrmLead(
        id=uuid4(),
        tenant_id=repo.tenant_id,
        full_name=item.full_name,
        phone=item.phone,
        email=item.email,
        source=item.source.value,
        status=item.status.value,
        assigned_to=item.assigned_to,
        notes=item.notes,
    )
    ctx.db.add(lead)
    _activity(ctx, lead.id, "note", "Lead created")
    try:
        await ctx.db.flush()
    except DBAPIError as exc:
        raise _conflict(exc) from exc
    await _audit(ctx, "crm_lead_created", lead.id, {"source": lead.source, "status": lead.status})
    return lead_dict(lead)


@router.get("/leads/{lead_id}")
async def get_lead(lead_id: UUID, ctx: RequestContext = VIEW) -> dict[str, Any]:
    lead = await _lead_or_404(ctx, lead_id)
    return {**lead_dict(lead), "activities": await repo.activities(ctx.db, lead_id)}


@router.patch("/leads/{lead_id}")
async def update_lead(
    lead_id: UUID,
    item: CrmLeadUpdate,
    ctx: RequestContext = MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    lead = await _lead_or_404(ctx, lead_id)
    data = item.model_dump(exclude_unset=True)
    old_status, old_assignee = lead.status, lead.assigned_to
    for key, value in data.items():
        setattr(lead, key, value.value if hasattr(value, "value") else value)
    lead.updated_at = datetime.now(UTC)
    if "status" in data and lead.status != old_status:
        _activity(ctx, lead.id, "status_change", f"Status: {old_status} → {lead.status}")
    if "assigned_to" in data and lead.assigned_to != old_assignee:
        _activity(ctx, lead.id, "assignment", f"Assigned to {lead.assigned_to or '—'}")
    try:
        await ctx.db.flush()
    except DBAPIError as exc:
        raise _conflict(exc) from exc
    await _audit(ctx, "crm_lead_updated", lead.id, {"changed": sorted(data)})
    return lead_dict(lead)


@router.delete("/leads/{lead_id}")
async def delete_lead(
    lead_id: UUID, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    lead = await _lead_or_404(ctx, lead_id)
    await ctx.db.execute(delete(CrmLead).where(CrmLead.id == lead.id))
    await _audit(
        ctx, "crm_lead_deleted", lead.id, {"full_name": lead.full_name, "status": lead.status}
    )
    return {"deleted": True}


@router.post("/leads/bulk-assign")
async def bulk_assign(
    item: CrmBulkAssign, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
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


@router.post("/leads/bulk-status")
async def bulk_status(
    item: CrmBulkStatus, ctx: RequestContext = MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    leads = await repo.leads_by_ids(ctx.db, item.ids)
    for lead in leads:
        previous = lead.status
        lead.status = item.status.value
        lead.updated_at = datetime.now(UTC)
        _activity(ctx, lead.id, "status_change", f"Status: {previous} → {lead.status}")
        await _audit(ctx, "crm_lead_bulk_status", lead.id, {"from": previous, "to": lead.status})
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
        lead_id=lead_id,
        type=item.type.value,
        description=item.description,
        created_by=ctx.actor.id,
    )
    ctx.db.add(row)
    await ctx.db.flush()
    await _audit(ctx, "crm_lead_activity", lead_id, {"type": row.type})
    return activity_dict(row)


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
            full_name=candidate.full_name,
            phone=candidate.phone,
            email=candidate.email,
            source=candidate.source,
            status="new",
            notes=candidate.notes,
            import_batch_id=batch.id,
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


# --- Dashboard -------------------------------------------------------------


@router.get("/stats")
async def stats(ctx: RequestContext = VIEW) -> dict[str, Any]:
    return await repo.stats(ctx.db)
