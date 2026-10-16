"""Scope-safe administration for teams, auto-assignment and integration settings."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, require_permission
from app.domain.enums import OwnershipLevel
from app.domain.schemas import (
    AssignmentRuleSpec,
    IntegrationConfigSpec,
    ReportScheduleSpec,
    TeamSpec,
)
from app.repositories.admin import AdminRepository
from app.security.tokens import csrf_required

router = APIRouter(prefix="/api/staff/admin", tags=["administration"])


def require_global_admin_scope(ctx: RequestContext) -> None:
    if (
        not ctx.scope.all_companies
        or ctx.scope.company_ids
        or ctx.scope.insurance_types
        or ctx.scope.ownership != OwnershipLevel.ALL
    ):
        raise HTTPException(status_code=403, detail="Global administration scope required")


def db_error(exc: DBAPIError) -> HTTPException:
    state = getattr(exc.orig, "sqlstate", "")
    if state == "23505":
        return HTTPException(status_code=409, detail="A record with that name already exists")
    if state == "22023":
        return HTTPException(status_code=422, detail="The administration data is invalid")
    return HTTPException(status_code=403, detail="Administration change forbidden")


@router.get("/access-options")
async def access_options(
    ctx: RequestContext = Depends(require_permission("users.manage_access")),
) -> dict[str, Any]:
    require_global_admin_scope(ctx)
    result = await AdminRepository().access_options(ctx.db)
    if result is None:
        raise HTTPException(status_code=403, detail="Global access administration required")
    return result


@router.get("/teams")
async def teams(
    ctx: RequestContext = Depends(require_permission("teams.manage")),
) -> list[dict[str, Any]]:
    require_global_admin_scope(ctx)
    result = await AdminRepository().teams(ctx.db)
    if result is None:
        raise HTTPException(status_code=403, detail="Global team management required")
    return result


@router.post("/teams", status_code=201)
async def create_team(
    item: TeamSpec,
    ctx: RequestContext = Depends(require_permission("teams.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    require_global_admin_scope(ctx)
    try:
        result = await AdminRepository().save_team(ctx.db, None, item)
    except DBAPIError as exc:
        raise db_error(exc) from exc
    if result is None:
        raise HTTPException(status_code=422, detail="Team could not be created")
    return {"id": result}


@router.put("/teams/{team_id}")
async def update_team(
    team_id: UUID,
    item: TeamSpec,
    ctx: RequestContext = Depends(require_permission("teams.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    require_global_admin_scope(ctx)
    try:
        result = await AdminRepository().save_team(ctx.db, team_id, item)
    except DBAPIError as exc:
        raise db_error(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Team not found")
    return {"id": result}


@router.get("/automation-options")
async def automation_options(
    ctx: RequestContext = Depends(require_permission("settings.manage")),
) -> dict[str, Any]:
    require_global_admin_scope(ctx)
    result = await AdminRepository().assignment_options(ctx.db)
    if result is None:
        raise HTTPException(status_code=403, detail="Global automation management required")
    return result


@router.get("/assignment-rules")
async def assignment_rules(
    ctx: RequestContext = Depends(require_permission("settings.manage")),
) -> list[dict[str, Any]]:
    require_global_admin_scope(ctx)
    result = await AdminRepository().assignment_rules(ctx.db)
    if result is None:
        raise HTTPException(status_code=403, detail="Global automation management required")
    return result


@router.post("/assignment-rules", status_code=201)
async def create_assignment_rule(
    item: AssignmentRuleSpec,
    ctx: RequestContext = Depends(require_permission("settings.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    require_global_admin_scope(ctx)
    try:
        result = await AdminRepository().save_assignment_rule(ctx.db, None, item)
    except DBAPIError as exc:
        raise db_error(exc) from exc
    if result is None:
        raise HTTPException(status_code=422, detail="Assignment rule could not be created")
    return {"id": result}


@router.put("/assignment-rules/{rule_id}")
async def update_assignment_rule(
    rule_id: UUID,
    item: AssignmentRuleSpec,
    ctx: RequestContext = Depends(require_permission("settings.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    require_global_admin_scope(ctx)
    try:
        result = await AdminRepository().save_assignment_rule(ctx.db, rule_id, item)
    except DBAPIError as exc:
        raise db_error(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Assignment rule not found")
    return {"id": result}


@router.get("/report-schedules")
async def report_schedules(
    ctx: RequestContext = Depends(require_permission("reports.manage")),
) -> dict[str, Any]:
    return {
        "items": await AdminRepository().report_schedules(ctx.db, ctx.actor.id),
        "delivery_ready": False,
    }


@router.post("/report-schedules", status_code=201)
async def create_report_schedule(
    item: ReportScheduleSpec,
    ctx: RequestContext = Depends(require_permission("reports.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    try:
        result = await AdminRepository().save_report_schedule(ctx.db, ctx.actor.id, None, item)
    except DBAPIError as exc:
        raise db_error(exc) from exc
    if result is None:
        raise HTTPException(status_code=422, detail="Report schedule could not be saved")
    return {"id": result, "delivery_ready": False}


@router.put("/report-schedules/{schedule_id}")
async def update_report_schedule(
    schedule_id: UUID,
    item: ReportScheduleSpec,
    ctx: RequestContext = Depends(require_permission("reports.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    try:
        result = await AdminRepository().save_report_schedule(
            ctx.db, ctx.actor.id, schedule_id, item
        )
    except DBAPIError as exc:
        raise db_error(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Report schedule not found")
    return {"id": result, "delivery_ready": False}


@router.get("/integrations")
async def integrations(
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
) -> list[dict[str, Any]]:
    require_global_admin_scope(ctx)
    result = await AdminRepository().integrations(ctx.db)
    if result is None:
        raise HTTPException(status_code=403, detail="Global integration management required")
    return result


@router.put("/integrations/{provider}")
async def update_integration(
    provider: Literal["email", "whatsapp"],
    item: IntegrationConfigSpec,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    require_global_admin_scope(ctx)
    try:
        saved = await AdminRepository().save_integration(ctx.db, provider, item)
    except DBAPIError as exc:
        raise db_error(exc) from exc
    if not saved:
        raise HTTPException(status_code=422, detail="Integration configuration was not saved")
    return {"saved": True, "runtime_ready": False}
