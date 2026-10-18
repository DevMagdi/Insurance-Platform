"""Customer complaint intake, tracking and strictly owned workflows."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.deps import RequestContext, client_ip, integration_service, require_customer
from app.db import purpose_transaction
from app.domain.schemas import (
    ComplaintCreate,
    ComplaintFilter,
    CsatRequest,
    CustomerProfileUpdate,
    GuestTrackRequest,
    NotificationPreferences,
    ReplyRequest,
    VoiceConsentRequest,
)
from app.models.identity import InsuranceCompany
from app.observability import metrics
from app.repositories.complaints import ComplaintRepository
from app.repositories.profile import CustomerProfileRepository
from app.security.captcha import captcha_guard
from app.security.tokens import csrf_required
from app.services.realtime import stream_customer_complaint

router = APIRouter(prefix="/api", tags=["customer"])


COMPANIES_CACHE_KEY = "cache:public:companies:v1"


@router.get("/companies")
async def public_companies(request: Request, response: Response) -> list[dict[str, Any]]:
    await request.app.state.auth.limits.check("companies:ip", client_ip(request), 120, 60)
    # Non-personal reference data: shareable by browsers and the CDN.
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=60"
    state_redis = getattr(request.app.state, "redis", None)
    redis = state_redis if isinstance(state_redis, Redis) else None
    cached = await redis.get(COMPANIES_CACHE_KEY) if redis is not None else None
    if cached:
        metrics.CACHE_REQUESTS.labels(cache="companies", result="hit").inc()
        return json.loads(cached)  # type: ignore[no-any-return]
    metrics.CACHE_REQUESTS.labels(cache="companies", result="miss").inc()
    companies = await _load_companies(request)
    if redis is not None:
        await redis.set(COMPANIES_CACHE_KEY, json.dumps(companies, default=str), ex=60)
    return companies


async def _load_companies(request: Request) -> list[dict[str, Any]]:
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


@router.post("/guest/track", dependencies=[Depends(captcha_guard("guest_track"))])
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


@router.get("/customer/profile")
async def customer_profile(
    ctx: RequestContext = Depends(require_customer),
) -> dict[str, Any]:
    profile = await CustomerProfileRepository().profile(ctx.db)
    if profile is None:
        raise HTTPException(status_code=404, detail="Customer unavailable")
    return profile


@router.patch("/customer/profile")
async def update_customer_profile(
    item: CustomerProfileUpdate,
    ctx: RequestContext = Depends(require_customer),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    repository = CustomerProfileRepository()
    # True PATCH semantics: a field the client did not send keeps its stored
    # value. The SECURITY DEFINER helper still validates the merged payload, so
    # no invariant (name length, email shape, preference keys) is bypassed.
    sent = item.model_fields_set
    if not {"full_name", "email", "whatsapp_available", "notification_preferences"} <= sent:
        current = await repository.profile(ctx.db)
        if current is None:
            raise HTTPException(status_code=404, detail="Customer unavailable")
        merge: dict[str, Any] = {}
        if "full_name" not in sent or item.full_name is None:
            merge["full_name"] = current["full_name"]
        if "email" not in sent:
            merge["email"] = current["email"]
        if "whatsapp_available" not in sent:
            merge["whatsapp_available"] = current["whatsapp_available"]
        if "notification_preferences" not in sent:
            merge["notification_preferences"] = NotificationPreferences.model_validate(
                current["notification_preferences"]
            )
        item = item.model_copy(update=merge)
    try:
        profile = await repository.update(ctx.db, item)
    except Exception as exc:  # The definer raises 22023 for invalid payloads.
        if "22023" in str(exc) or "invalid customer profile" in str(exc):
            raise HTTPException(status_code=422, detail="Invalid customer profile") from exc
        raise
    if profile is None:
        raise HTTPException(status_code=404, detail="Customer unavailable")
    return profile


@router.get("/customer/complaints/{complaint_id}/stream")
async def customer_complaint_stream(complaint_id: UUID, request: Request) -> StreamingResponse:
    """SSE ticket updates; cookie auth is revalidated on every poll (see realtime)."""
    auth = request.app.state.auth
    claims = auth.tokens.parse(request.cookies.get("access_token"), "access")
    if claims.subject.scope != "customer":
        raise HTTPException(status_code=403, detail="Customer account required")
    generator = stream_customer_complaint(
        factory=request.app.state.factory,
        redis=request.app.state.redis,
        complaints=request.app.state.complaints,
        claims=claims,
        complaint_id=complaint_id,
        is_disconnected=request.is_disconnected,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/customer/notifications")
async def customer_notifications(
    ctx: RequestContext = Depends(require_customer),
) -> list[dict[str, Any]]:
    records = await ComplaintRepository().notifications(ctx.db, ctx.scope)
    return [
        {
            "id": item.id,
            "kind": item.kind,
            "payload": item.payload,
            "created_at": item.created_at,
            "read_at": item.read_at,
        }
        for item in records
    ]


@router.patch("/customer/notifications/{notification_id}/read")
async def read_customer_notification(
    notification_id: UUID,
    ctx: RequestContext = Depends(require_customer),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    if not await CustomerProfileRepository().mark_notification_read(ctx.db, notification_id):
        raise HTTPException(status_code=404, detail="Notification unavailable")
    return {"read": True}
