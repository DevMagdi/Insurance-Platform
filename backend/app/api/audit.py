"""RLS-filtered audit timelines and bounded metadata-only CSV exports."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.deps import RequestContext, require_permission
from app.domain.enums import OwnershipLevel
from app.repositories.audit import AuditRepository

router = APIRouter(prefix="/api/staff/audit", tags=["audit"])


def window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    to = end or datetime.now(UTC)
    since = start or (to - timedelta(days=30))
    if (
        since.tzinfo is None
        or to.tzinfo is None
        or since.utcoffset() is None
        or to.utcoffset() is None
        or since > to
        or to - since > timedelta(days=366)
    ):
        raise HTTPException(
            status_code=422, detail="Audit dates require a bounded timezone-aware window"
        )
    return since, to


def require_global_access_audit(ctx: RequestContext) -> None:
    if (
        not ctx.scope.all_companies
        or ctx.scope.insurance_types
        or ctx.scope.ownership != OwnershipLevel.ALL
    ):
        raise HTTPException(status_code=403, detail="Global access audit required")


def safe_csv_cell(value: Any) -> str:
    cell = str(value) if value is not None else ""
    return "'" + cell if cell.lstrip().startswith(("=", "+", "-", "@")) else cell


@router.get("/events")
async def audit_events(
    start: datetime | None = None,
    end: datetime | None = None,
    actor_id: UUID | None = None,
    complaint_id: UUID | None = None,
    action: str | None = Query(default=None, min_length=1, max_length=80),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=10000),
    ctx: RequestContext = Depends(require_permission("audit.view")),
) -> list[dict[str, Any]]:
    since, to = window(start, end)
    return await AuditRepository().events(
        ctx.db,
        since,
        to,
        limit,
        offset,
        actor_id=actor_id,
        complaint_id=complaint_id,
        action=action,
    )


@router.get("/access-changes")
async def access_changes(
    target_id: UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=10000),
    ctx: RequestContext = Depends(require_permission("audit.view")),
) -> list[dict[str, Any]]:
    require_permission("users.manage_access").verify(ctx)
    require_global_access_audit(ctx)
    since, to = window(start, end) if start is not None or end is not None else (None, None)
    return await AuditRepository().access_changes(ctx.db, target_id, limit, offset, since, to)


@router.get("/events.csv")
async def export_audit(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    actor_id: UUID | None = None,
    complaint_id: UUID | None = None,
    action: str | None = Query(default=None, min_length=1, max_length=80),
    ctx: RequestContext = Depends(require_permission("audit.view")),
) -> Response:
    since, to = window(start, end)
    await request.app.state.auth.limits.check("audit:export", str(ctx.actor.id), 5, 300)
    rows = await AuditRepository().events(
        ctx.db, since, to, 2000, actor_id=actor_id, complaint_id=complaint_id, action=action
    )
    buffer = io.StringIO(newline="")
    fields = (
        "id",
        "created_at",
        "actor_staff_id",
        "action",
        "resource_type",
        "resource_id",
        "complaint_id",
    )
    writer = csv.writer(buffer)
    writer.writerow(fields)
    for row in rows:
        writer.writerow([safe_csv_cell(row[key]) for key in fields])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="audit-events.csv"',
            "Cache-Control": "no-store",
            "X-Export-Limit": "2000",
        },
    )


@router.get("/access-changes.csv")
async def export_access_changes(
    request: Request,
    target_id: UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    ctx: RequestContext = Depends(require_permission("audit.view")),
) -> Response:
    require_permission("users.manage_access").verify(ctx)
    require_global_access_audit(ctx)
    since, to = window(start, end)
    await request.app.state.auth.limits.check("audit:access-export", str(ctx.actor.id), 5, 300)
    rows = await AuditRepository().access_changes(ctx.db, target_id, 2000, 0, since, to)
    buffer = io.StringIO(newline="")
    fields = (
        "id",
        "created_at",
        "actor_staff_id",
        "target_user_id",
        "change_type",
        "before_state",
        "after_state",
    )
    writer = csv.writer(buffer)
    writer.writerow(fields)
    for row in rows:
        cells: list[Any] = []
        for field in fields:
            value = row[field]
            if field in {"before_state", "after_state"} and value is not None:
                value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            cells.append(safe_csv_cell(value))
        writer.writerow(cells)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="access-changes.csv"',
            "Cache-Control": "no-store",
            "X-Export-Limit": "2000",
        },
    )
