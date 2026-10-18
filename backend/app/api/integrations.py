"""Scoped ticket channels, AI review, voice scheduling and unified inbox."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import select

from app.api.deps import RequestContext, integration_service, require_permission
from app.domain.schemas import (
    AIAnalysisRequest,
    AIReviewRequest,
    CustomerSmsRequest,
    ReplyRequest,
    SendComplaintEmailRequest,
    VoiceFollowupRequest,
    WhatsAppSendRequest,
)
from app.models.tickets import AuditLog, CallLog
from app.security.blobs import RECORDING, BlobCipher
from app.security.tokens import csrf_required
from app.services.backpressure import backpressure

router = APIRouter(prefix="/api/staff", tags=["integrations"])


@router.post(
    "/complaints/{complaint_id}/email",
    status_code=202,
    dependencies=[Depends(backpressure("integration"))],
)
async def send_complaint_email(
    complaint_id: UUID,
    item: SendComplaintEmailRequest,
    request: Request,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    ),
    ctx: RequestContext = Depends(require_permission("integrations.send_email")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await integration_service(request).send_complaint_by_email(
        ctx.db, ctx.scope, complaint_id, item, idempotency_key
    )


@router.post(
    "/complaints/{complaint_id}/email/reply",
    status_code=202,
    dependencies=[Depends(backpressure("integration"))],
)
async def send_customer_email_reply(
    complaint_id: UUID,
    item: ReplyRequest,
    request: Request,
    locale: Literal["ar", "en"] = "en",
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    ),
    ctx: RequestContext = Depends(require_permission("integrations.send_email")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await integration_service(request).send_customer_email(
        ctx.db, ctx.scope, complaint_id, item.body, locale, idempotency_key
    )


@router.post(
    "/complaints/{complaint_id}/sms",
    status_code=202,
    dependencies=[Depends(backpressure("integration"))],
)
async def send_customer_sms_message(
    complaint_id: UUID,
    item: CustomerSmsRequest,
    request: Request,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    ),
    ctx: RequestContext = Depends(require_permission("integrations.send_sms")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await integration_service(request).send_customer_sms(
        ctx.db, ctx.scope, complaint_id, item.body, idempotency_key
    )


@router.post(
    "/complaints/{complaint_id}/whatsapp",
    status_code=202,
    dependencies=[Depends(backpressure("integration"))],
)
async def send_whatsapp_message(
    complaint_id: UUID,
    item: WhatsAppSendRequest,
    request: Request,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    ),
    ctx: RequestContext = Depends(require_permission("integrations.send_whatsapp")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await integration_service(request).send_whatsapp(
        ctx.db, ctx.scope, complaint_id, item, idempotency_key
    )


@router.post(
    "/complaints/{complaint_id}/ai/analyze",
    status_code=202,
    dependencies=[Depends(backpressure("integration"))],
)
async def request_ai_analysis(
    complaint_id: UUID,
    item: AIAnalysisRequest,
    request: Request,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    ),
    ctx: RequestContext = Depends(require_permission("integrations.ai_call")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await integration_service(request).request_ai_analysis(
        ctx.db, ctx.scope, complaint_id, item, idempotency_key
    )


@router.get("/complaints/{complaint_id}/ai/insights")
async def ai_insights(
    complaint_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("integrations.ai_call")),
) -> list[dict[str, Any]]:
    return await integration_service(request).list_ai_insights(ctx.db, ctx.scope, complaint_id)


@router.get("/complaints/{complaint_id}/ai/similar")
async def similar_complaints(
    complaint_id: UUID,
    request: Request,
    limit: int = 10,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> list[dict[str, Any]]:
    return await integration_service(request).similar_tickets(
        ctx.db, ctx.scope, complaint_id, limit
    )


@router.post("/complaints/{complaint_id}/ai/insights/{insight_id}/review")
async def review_ai_suggestion(
    complaint_id: UUID,
    insight_id: UUID,
    item: AIReviewRequest,
    request: Request,
    ctx: RequestContext = Depends(require_permission("integrations.ai_call")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await integration_service(request).review_ai_insight(
        ctx.db, ctx.scope, complaint_id, insight_id, item.accept
    )


@router.post(
    "/complaints/{complaint_id}/ai/insights/{insight_id}/send",
    status_code=202,
    dependencies=[Depends(backpressure("integration"))],
)
async def send_approved_ai_reply(
    complaint_id: UUID,
    insight_id: UUID,
    request: Request,
    channel: Literal["email", "whatsapp"],
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    ),
    ctx: RequestContext = Depends(require_permission("integrations.ai_call")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    permission = "integrations.send_email" if channel == "email" else "integrations.send_whatsapp"
    require_permission(permission).verify(ctx)
    return await integration_service(request).send_approved_reply(
        ctx.db, ctx.scope, complaint_id, insight_id, channel, idempotency_key
    )


@router.get("/complaints/{complaint_id}/inbox")
async def unified_inbox(
    complaint_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> dict[str, Any]:
    return await integration_service(request).unified_inbox(
        ctx.db,
        ctx.scope,
        complaint_id,
        include_calls="integrations.ai_call" in ctx.permissions,
    )


@router.post(
    "/complaints/{complaint_id}/voice-followups",
    status_code=202,
    dependencies=[Depends(backpressure("integration"))],
)
async def schedule_voice_followup(
    complaint_id: UUID,
    item: VoiceFollowupRequest,
    request: Request,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    ),
    ctx: RequestContext = Depends(require_permission("integrations.ai_call")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await integration_service(request).schedule_voice_followup(
        ctx.db, ctx.scope, complaint_id, item, idempotency_key
    )


@router.get("/complaints/{complaint_id}/calls/{call_id}/recording")
async def call_recording(
    complaint_id: UUID,
    call_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("integrations.ai_call")),
) -> Response:
    call = await ctx.db.scalar(
        select(CallLog).where(
            CallLog.id == call_id,
            CallLog.complaint_id == complaint_id,
        )
    )
    if call is None or call.recording_content is None:
        raise HTTPException(status_code=404, detail="Call recording unavailable")
    # Recordings are encrypted rows in the platform database, so this route is the
    # only way to the bytes: scope, permission and audit all apply first.
    blobs: BlobCipher = request.app.state.blobs
    try:
        audio = blobs.decrypt(call.recording_content, purpose=RECORDING, resource_id=call.id)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Call recording could not be read") from exc
    ctx.db.add(
        AuditLog(
            actor_staff_id=ctx.actor.id,
            action="call_recording_accessed",
            resource_type="call_log",
            resource_id=call.id,
            complaint_id=call.complaint_id,
            complaint_created_at=call.complaint_created_at,
        )
    )
    await ctx.db.flush()
    return Response(
        content=audio,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="call-{call.id}.audio"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/integration-jobs/{job_id}")
async def integration_job_status(
    job_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("complaints.view")),
) -> dict[str, Any]:
    return await integration_service(request).status(ctx.db, job_id)
