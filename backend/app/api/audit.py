"""RLS-filtered audit timelines and bounded metadata-only CSV exports."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.deps import RequestContext, require_permission
from app.domain.enums import OwnershipLevel
from app.repositories.audit import AuditRepository
from app.services.render import MEDIA_TYPES, tabular_bytes

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


@router.get("/events")
async def audit_events(
    start: datetime | None = None,
    end: datetime | None = None,
    actor_id: UUID | None = None,
    complaint_id: UUID | None = None,
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
    )


@router.get("/access-changes")
async def access_changes(
    target_id: UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=10000),
    ctx: RequestContext = Depends(require_permission("audit.view")),
) -> list[dict[str, Any]]:
    require_permission("users.manage_access").verify(ctx)
    if (
        not ctx.scope.all_companies
        or ctx.scope.insurance_types
        or ctx.scope.ownership != OwnershipLevel.ALL
    ):
        raise HTTPException(status_code=403, detail="Global access audit required")
    return await AuditRepository().access_changes(ctx.db, target_id, limit, offset)


AUDIT_FIELDS = (
    "id",
    "created_at",
    "actor_staff_id",
    "action",
    "resource_type",
    "resource_id",
    "complaint_id",
)


async def audit_rows(
    request: Request,
    ctx: RequestContext,
    start: datetime | None,
    end: datetime | None,
    actor_id: UUID | None,
    complaint_id: UUID | None,
) -> list[dict[str, Any]]:
    """Both audit exports share one bounded, scope-filtered row set."""
    since, to = window(start, end)
    await request.app.state.auth.limits.check("audit:export", str(ctx.actor.id), 5, 300)
    return await AuditRepository().events(
        ctx.db, since, to, 2000, actor_id=actor_id, complaint_id=complaint_id
    )


@router.get("/events.xlsx")
async def export_audit_xlsx(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    actor_id: UUID | None = None,
    complaint_id: UUID | None = None,
    ctx: RequestContext = Depends(require_permission("audit.view")),
) -> Response:
    rows = await audit_rows(request, ctx, start, end, actor_id, complaint_id)
    data = tabular_bytes(
        "xlsx", "audit", AUDIT_FIELDS, [[row[key] for key in AUDIT_FIELDS] for row in rows]
    )
    return Response(
        content=data,
        media_type=MEDIA_TYPES["xlsx"],
        headers={
            "Content-Disposition": 'attachment; filename="audit-events.xlsx"',
            "Cache-Control": "no-store",
            "X-Export-Limit": "2000",
        },
    )


@router.get("/events.csv")
async def export_audit(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    actor_id: UUID | None = None,
    complaint_id: UUID | None = None,
    ctx: RequestContext = Depends(require_permission("audit.view")),
) -> Response:
    rows = await audit_rows(request, ctx, start, end, actor_id, complaint_id)
    buffer = io.StringIO(newline="")
    fields = AUDIT_FIELDS
    writer = csv.writer(buffer)
    writer.writerow(fields)
    for row in rows:
        cells = [str(row[key]) if row[key] is not None else "" for key in fields]
        writer.writerow(
            ["'" + cell if cell.startswith(("=", "+", "-", "@")) else cell for cell in cells]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="audit-events.csv"',
            "Cache-Control": "no-store",
            "X-Export-Limit": "2000",
        },
    )
