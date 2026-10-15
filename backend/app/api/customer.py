"""Customer complaint intake, tracking and strictly owned workflows."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import select

from app.api.deps import RequestContext, client_ip, integration_service, require_customer
from app.db import purpose_transaction
from app.domain.schemas import (
    ComplaintCreate,
    ComplaintFilter,
    CsatRequest,
    GuestTrackRequest,
    ReplyRequest,
    VoiceConsentRequest,
)
from app.models.identity import InsuranceCompany
from app.repositories.complaints import ComplaintRepository
from app.security.tokens import csrf_required

router = APIRouter(prefix="/api", tags=["customer"])


@router.get("/companies")
async def public_companies(request: Request) -> list[dict[str, Any]]:
    await request.app.state.auth.limits.check("companies:ip", client_ip(request), 120, 60)
    async with purpose_transaction(request.app.state.factory, "guest") as db:
        result = await db.execute(
            select(
                InsuranceCompany.id,
                InsuranceCompany.name_en,
                InsuranceCompany.name_ar,
                InsuranceCompany.supported_types,
            )
            .where(InsuranceCompany.is_active.is_(True))
            .order_by(InsuranceCompany.name_en)
        )
        return [
            {
                "id": row.id,
                "name_en": row.name_en,
                "name_ar": row.name_ar,
                "insurance_types": row.supported_types,
            }
            for row in result
        ]


@router.post("/guest/track")
async def guest_track(item: GuestTrackRequest, request: Request) -> dict[str, Any]:
    auth = request.app.state.auth
    await auth.limits.check("guest:ip", client_ip(request), 15, 600)
    blind = auth.protector.blind_index(item.national_id)
    await auth.limits.check("guest:pair", f"{item.ticket_no}:{blind.hex()}", 5, 600)
    async with purpose_transaction(request.app.state.factory, "guest") as db:
        row = await ComplaintRepository().guest_track(db, item.ticket_no, blind)
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket and national ID pair not found")
    return row


@router.post("/customer/complaints", status_code=201)
async def create_complaint(
    item: ComplaintCreate,
    request: Request,
    response: Response,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    ),
    ctx: RequestContext = Depends(require_customer),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    await request.app.state.auth.limits.check("complaints:create", str(ctx.actor.id), 12, 3600)
    await request.app.state.auth.limits.check("complaints:create:ip", client_ip(request), 60, 3600)
    complaint, replay = await ctx.complaints.create(ctx.db, ctx.scope, item, idempotency_key)
    if replay:
        response.status_code = 200
    return {**ctx.complaints.summary(complaint), "replayed": replay}


@router.get("/customer/complaints")
async def customer_complaints(
    filters: ComplaintFilter = Query(), ctx: RequestContext = Depends(require_customer)
) -> dict[str, Any]:
    return await ctx.complaints.list(ctx.db, ctx.scope, filters)


@router.get("/customer/complaints/counters")
async def customer_counters(
    filters: ComplaintFilter = Query(), ctx: RequestContext = Depends(require_customer)
) -> dict[str, int]:
    return await ComplaintRepository().counters(ctx.db, ctx.scope, filters)


@router.get("/customer/complaints/{complaint_id}")
async def customer_detail(
    complaint_id: UUID, ctx: RequestContext = Depends(require_customer)
) -> dict[str, Any]:
    return await ctx.complaints.detail(ctx.db, ctx.scope, complaint_id)


@router.get("/customer/complaints/{complaint_id}/timeline")
async def customer_timeline(
    complaint_id: UUID,
    page: int = 1,
    page_size: int = 50,
    ctx: RequestContext = Depends(require_customer),
) -> dict[str, Any]:
    if not 1 <= page <= 100 or not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="Invalid timeline page")
    parent = await ctx.complaints.parent(ctx.db, ctx.scope, complaint_id)
    return await ComplaintRepository().timeline(ctx.db, ctx.scope, parent, page, page_size)


@router.post("/customer/complaints/{complaint_id}/reply")
async def customer_reply(
    complaint_id: UUID,
    item: ReplyRequest,
    ctx: RequestContext = Depends(require_customer),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await ctx.complaints.reply(ctx.db, ctx.scope, complaint_id, item.body)


@router.post("/customer/complaints/{complaint_id}/voice-consent")
async def customer_voice_consent(
    complaint_id: UUID,
    item: VoiceConsentRequest,
    request: Request,
    ctx: RequestContext = Depends(require_customer),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await integration_service(request).record_voice_consent(
        ctx.db, ctx.scope, complaint_id, item.enabled
    )


@router.post("/customer/complaints/{complaint_id}/reopen")
async def customer_reopen(
    complaint_id: UUID,
    ctx: RequestContext = Depends(require_customer),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await ctx.complaints.reopen(ctx.db, ctx.scope, complaint_id)


@router.post("/customer/complaints/{complaint_id}/csat")
async def customer_csat(
    complaint_id: UUID,
    item: CsatRequest,
    ctx: RequestContext = Depends(require_customer),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await ctx.complaints.csat(ctx.db, ctx.scope, complaint_id, item.score, item.comment)


@router.get("/customer/complaints/{complaint_id}/attachments")
async def customer_attachments(
    complaint_id: UUID, ctx: RequestContext = Depends(require_customer)
) -> list[dict[str, Any]]:
    parent = await ctx.complaints.parent(ctx.db, ctx.scope, complaint_id)
    records = await ComplaintRepository().attachments(ctx.db, ctx.scope, parent)
    return [
        {
            "id": item.id,
            "filename": item.original_filename,
            "mime_type": item.mime_type,
            "size_bytes": item.size_bytes,
            "scan_status": item.scan_status,
        }
        for item in records
    ]


@router.get("/customer/notifications")
async def customer_notifications(
    ctx: RequestContext = Depends(require_customer),
) -> list[dict[str, Any]]:
    records = await ComplaintRepository().notifications(ctx.db, ctx.scope)
    return [
        {"id": item.id, "kind": item.kind, "payload": item.payload, "created_at": item.created_at}
        for item in records
    ]
