"""Administration of provider integrations, assignment rules and duplicates."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, require_permission, require_staff
from app.api.staff import access_error
from app.domain.schemas import AssignmentPreview, AssignmentRuleSpec, IntegrationSpec
from app.repositories.integrations import (
    AssignmentRuleRepository,
    DuplicateCandidateRepository,
    IntegrationRepository,
)
from app.security.tokens import csrf_required

router = APIRouter(prefix="/api/staff/admin", tags=["administration"])


@router.get("/integrations")
async def integrations(
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
) -> list[dict[str, Any]]:
    settings = await IntegrationRepository().settings(ctx.db)
    if settings is None:
        raise HTTPException(status_code=403, detail="Integration management required")
    return settings


@router.post("/integrations", status_code=201)
async def save_integration(
    item: IntegrationSpec,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    try:
        item_id = await IntegrationRepository().save(ctx.db, None, item)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if item_id is None:
        raise HTTPException(status_code=404, detail="Integration unavailable")
    return {"id": item_id}


@router.put("/integrations/{item_id}")
async def update_integration(
    item_id: UUID,
    item: IntegrationSpec,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    try:
        saved = await IntegrationRepository().save(ctx.db, item_id, item)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if saved is None:
        raise HTTPException(status_code=404, detail="Integration unavailable")
    return {"id": saved}


@router.delete("/integrations/{item_id}", status_code=204)
async def delete_integration(
    item_id: UUID,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> None:
    try:
        removed = await IntegrationRepository().delete(ctx.db, item_id)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Integration unavailable")


@router.get("/assignment-rules")
async def assignment_rules(
    ctx: RequestContext = Depends(require_permission("complaints.assign")),
) -> list[dict[str, Any]]:
    return await AssignmentRuleRepository().rules(ctx.db, ctx.scope)


@router.post("/assignment-rules", status_code=201)
async def create_assignment_rule(
    item: AssignmentRuleSpec,
    ctx: RequestContext = Depends(require_permission("complaints.assign")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    try:
        rule_id = await AssignmentRuleRepository().save(ctx.db, None, item)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if rule_id is None:
        raise HTTPException(status_code=404, detail="Assignment rule unavailable")
    return {"id": rule_id}


@router.put("/assignment-rules/{item_id}")
async def update_assignment_rule(
    item_id: UUID,
    item: AssignmentRuleSpec,
    ctx: RequestContext = Depends(require_permission("complaints.assign")),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    try:
        saved = await AssignmentRuleRepository().save(ctx.db, item_id, item)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if saved is None:
        raise HTTPException(status_code=404, detail="Assignment rule unavailable")
    return {"id": saved}


@router.delete("/assignment-rules/{item_id}", status_code=204)
async def delete_assignment_rule(
    item_id: UUID,
    ctx: RequestContext = Depends(require_permission("complaints.assign")),
    _: None = Depends(csrf_required),
) -> None:
    try:
        removed = await AssignmentRuleRepository().delete(ctx.db, item_id)
    except DBAPIError as exc:
        raise access_error(exc) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Assignment rule unavailable")


@router.post("/assignment-rules/preview")
async def preview_assignment(
    item: AssignmentPreview,
    ctx: RequestContext = Depends(require_permission("complaints.assign")),
) -> dict[str, Any]:
    """Dry run: which in-scope agents the current rules would offer, in order."""
    result = await AssignmentRuleRepository().preview(ctx.db, item)
    if result is None:
        raise HTTPException(status_code=403, detail="Assignment preview not permitted")
    return result


@router.get("/duplicates")
async def duplicate_candidates(
    limit: int = 50,
    ctx: RequestContext = Depends(require_permission("complaints.merge")),
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="Invalid limit")
    return await DuplicateCandidateRepository().open_candidates(ctx.db, ctx.scope, limit)


@router.post("/duplicates/scan/{complaint_id}", status_code=202)
async def scan_duplicates(
    complaint_id: UUID,
    ctx: RequestContext = Depends(require_staff),
    _: None = Depends(csrf_required),
) -> dict[str, int]:
    """Re-scan a visible ticket on demand; the DB confirms the ticket is readable."""
    require_permission("complaints.merge").verify(ctx)
    parent = await ctx.complaints.parent(ctx.db, ctx.scope, complaint_id)
    inserted = await DuplicateCandidateRepository().scan_ticket(
        ctx.db, complaint_id, parent.created_at
    )
    return {"candidates": inserted}
