"""PDPL data-subject rights: consent log, data export and privacy requests.

Customers manage only their own records (RLS + definer functions); privacy
officers process requests with ``settings.manage`` and global scope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import RequestContext, client_ip, require_customer, require_permission, user_agent
from app.domain.privacy import ConsentUpdate, PrivacyRequestCreate, PrivacyRequestDecision
from app.observability import metrics
from app.repositories.privacy import PrivacyRepository
from app.repositories.profile import CustomerProfileRepository
from app.security.tokens import csrf_required

router = APIRouter(prefix="/api", tags=["privacy"])


def _policy_version(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return str(getattr(settings, "privacy_policy_version", "2026-10"))


@router.get("/customer/privacy/consents")
async def list_consents(ctx: RequestContext = Depends(require_customer)) -> list[dict[str, Any]]:
    return await PrivacyRepository().latest_consents(ctx.db)


@router.post("/customer/privacy/consents", status_code=201)
async def record_consent(
    item: ConsentUpdate,
    request: Request,
    ctx: RequestContext = Depends(require_customer),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    version = _policy_version(request)
    consent_id = await PrivacyRepository().record_consent(
        ctx.db, item.purpose.value, item.granted, version, client_ip(request), user_agent(request)
    )
    if consent_id is None:
        raise HTTPException(status_code=403, detail="Consent cannot be recorded")
    return {
        "id": consent_id,
        "purpose": item.purpose,
        "granted": item.granted,
        "policy_version": version,
    }


@router.get("/customer/privacy/export")
async def export_my_data(ctx: RequestContext = Depends(require_customer)) -> dict[str, Any]:
    """Right of access (Law 151/2020): a machine-readable copy of the customer's data."""
    repo = PrivacyRepository()
    metrics.SENSITIVE_ACTIONS.labels(action="privacy_export").inc()
    return {
        "generated_at": datetime.now(UTC),
        "profile": await CustomerProfileRepository().profile(ctx.db),
        "complaints": await repo.own_complaints(ctx.db),
        "consents": await repo.consent_history(ctx.db),
        "privacy_requests": await repo.requests(ctx.db),
    }


@router.get("/customer/privacy/requests")
async def my_requests(ctx: RequestContext = Depends(require_customer)) -> list[dict[str, Any]]:
    return await PrivacyRepository().requests(ctx.db)


@router.post("/customer/privacy/requests", status_code=202)
async def create_request(
    item: PrivacyRequestCreate,
    ctx: RequestContext = Depends(require_customer),
    _: None = Depends(csrf_required),
) -> dict[str, UUID]:
    request_id = await PrivacyRepository().create_request(ctx.db, item.kind.value, item.details)
    if request_id is None:
        raise HTTPException(status_code=403, detail="Privacy request cannot be created")
    metrics.SENSITIVE_ACTIONS.labels(action=f"privacy_request_{item.kind.value}").inc()
    return {"id": request_id}


@router.get("/staff/privacy/requests")
async def privacy_queue(
    status: str | None = Query(default=None, pattern="^(pending|in_progress|completed|rejected)$"),
    ctx: RequestContext = Depends(require_permission("settings.manage")),
) -> list[dict[str, Any]]:
    return await PrivacyRepository().requests(ctx.db, status, 200)


@router.post("/staff/privacy/requests/{request_id}")
async def decide_request(
    request_id: UUID,
    item: PrivacyRequestDecision,
    ctx: RequestContext = Depends(require_permission("settings.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    if not await PrivacyRepository().decide(ctx.db, request_id, item.status.value, item.resolution):
        raise HTTPException(status_code=404, detail="Privacy request unavailable")
    metrics.SENSITIVE_ACTIONS.labels(action=f"privacy_decision_{item.status.value}").inc()
    return {"updated": True}
