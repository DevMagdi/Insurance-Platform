"""Signed, short-lived API upload/download endpoints; browser sees only relative URLs."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from app.api.deps import RequestContext, require_customer, require_permission
from app.domain.schemas import UploadPlan
from app.security.tokens import csrf_required
from app.services.attachments import AttachmentService

router = APIRouter(prefix="/api", tags=["attachments"])


def attachments(request: Request) -> AttachmentService:
    service: AttachmentService = request.app.state.attachments
    return service


async def _plan(
    request: Request,
    ctx: RequestContext,
    complaint_id: UUID,
    item: UploadPlan,
) -> list[dict[str, Any]]:
    await request.app.state.auth.limits.check(
        "upload:plan", f"{ctx.actor.id}:{complaint_id}", 10, 3600
    )
    parent = await ctx.complaints.parent(ctx.db, ctx.scope, complaint_id)
    return await attachments(request).plan(
        ctx.db, ctx.scope, ctx.session_id, ctx.token_version, parent, item
    )


@router.post("/customer/complaints/{complaint_id}/attachments/plan")
async def customer_plan(
    complaint_id: UUID,
    item: UploadPlan,
    request: Request,
    ctx: RequestContext = Depends(require_customer),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return {"uploads": await _plan(request, ctx, complaint_id, item)}


@router.post("/staff/complaints/{complaint_id}/attachments/plan")
async def staff_plan(
    complaint_id: UUID,
    item: UploadPlan,
    request: Request,
    ctx: RequestContext = Depends(require_permission("complaints.view_attachments")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return {"uploads": await _plan(request, ctx, complaint_id, item)}


@router.put("/customer/attachments/uploads/{upload_id}/content")
async def customer_upload(
    upload_id: UUID,
    request: Request,
    upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
    ctx: RequestContext = Depends(require_customer),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await attachments(request).upload(
        ctx.db, ctx.scope, ctx.session_id, ctx.token_version, upload_id, upload_token, request
    )


@router.put("/staff/attachments/uploads/{upload_id}/content")
async def staff_upload(
    upload_id: UUID,
    request: Request,
    upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
    ctx: RequestContext = Depends(require_permission("complaints.view_attachments")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await attachments(request).upload(
        ctx.db, ctx.scope, ctx.session_id, ctx.token_version, upload_id, upload_token, request
    )


@router.get("/customer/attachments/{attachment_id}/authorize")
async def customer_authorize_download(
    attachment_id: UUID, request: Request, ctx: RequestContext = Depends(require_customer)
) -> dict[str, Any]:
    return await attachments(request).authorize_download(
        ctx.db, ctx.scope, ctx.session_id, ctx.token_version, attachment_id
    )


@router.get("/staff/attachments/{attachment_id}/authorize")
async def staff_authorize_download(
    attachment_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("complaints.view_attachments")),
) -> dict[str, Any]:
    return await attachments(request).authorize_download(
        ctx.db, ctx.scope, ctx.session_id, ctx.token_version, attachment_id
    )


async def _download(
    request: Request,
    ctx: RequestContext,
    attachment_id: UUID,
    token: str | None,
) -> Response:
    content, record = await attachments(request).download(
        ctx.db, ctx.scope, ctx.session_id, ctx.token_version, attachment_id, token
    )
    return Response(
        content=content,
        media_type=record.mime_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(record.original_filename, safe='')}"
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/customer/attachments/{attachment_id}/content")
async def customer_download(
    attachment_id: UUID,
    request: Request,
    download_token: str | None = Header(default=None, alias="X-Download-Token"),
    ctx: RequestContext = Depends(require_customer),
) -> Response:
    return await _download(request, ctx, attachment_id, download_token)


@router.get("/staff/attachments/{attachment_id}/content")
async def staff_download(
    attachment_id: UUID,
    request: Request,
    download_token: str | None = Header(default=None, alias="X-Download-Token"),
    ctx: RequestContext = Depends(require_permission("complaints.view_attachments")),
) -> Response:
    return await _download(request, ctx, attachment_id, download_token)
