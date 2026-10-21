"""The corporate client portal.

A company contracted with the brokerage signs in here with its own username
and password — it is not a staff account and not a customer account. It sees
exactly its own data: its contract (value, payment frequency, renewal date),
its documents, and the requests raised by its employees.

Every endpoint relies on row-level security for isolation rather than on
filtering in Python: the session's scope is ``corporate``, so
``app_security.corporate_client_id()`` resolves inside the database and the
RLS policies restrict each query to that one client.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.deps import RequestContext, client_ip, require_corporate, user_agent
from app.domain.schemas import CorporateLoginRequest, CorporatePasswordChange
from app.security.captcha import captcha_guard
from app.security.tokens import clear_auth_cookies, csrf_required, set_auth_cookies
from app.services.corporate import CorporateService

router = APIRouter(prefix="/api/corporate", tags=["corporate"])


def _service() -> CorporateService:
    return CorporateService()


@router.post("/login", dependencies=[Depends(captcha_guard("corporate_login"))])
async def corporate_login(
    item: CorporateLoginRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Sign in a corporate client user."""
    service = request.app.state.corporate_auth
    result = await service.login(item, client_ip(request), user_agent(request))
    tokens = result["tokens"]
    if tokens is None:
        clear_auth_cookies(response, request.app.state.auth.settings)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    set_auth_cookies(response, tokens.access, tokens.refresh, request.app.state.auth.settings)
    return {
        "next_step": "change_password" if result["must_change_password"] else "authenticated",
        "full_name": result["full_name"],
        "must_change_password": result["must_change_password"],
    }


@router.post("/password/change", dependencies=[Depends(csrf_required)])
async def corporate_change_password(
    item: CorporatePasswordChange,
    request: Request,
    ctx: RequestContext = Depends(require_corporate),
) -> dict[str, str]:
    await request.app.state.corporate_auth.change_password(ctx.db, ctx.actor, item)
    return {"next_step": "sign_in_with_new_password"}


@router.get("/overview")
async def corporate_overview(
    ctx: RequestContext = Depends(require_corporate),
    service: CorporateService = Depends(_service),
) -> dict[str, Any]:
    """Contract value, payment frequency, renewal date and request counters."""
    return await service.overview(ctx.db)


@router.get("/members")
async def corporate_members(
    search: str | None = Query(default=None, max_length=120),
    ctx: RequestContext = Depends(require_corporate),
    service: CorporateService = Depends(_service),
) -> dict[str, Any]:
    """The company's employees and their requests, searchable by name."""
    return await service.members(ctx.db, search)


@router.get("/documents")
async def corporate_documents(
    ctx: RequestContext = Depends(require_corporate),
    service: CorporateService = Depends(_service),
) -> dict[str, Any]:
    """Policies and contract documents the broker shared with this company."""
    return await service.directory(ctx.db)


@router.get("/documents/{document_id}")
async def corporate_document_download(
    document_id: UUID,
    ctx: RequestContext = Depends(require_corporate),
    service: CorporateService = Depends(_service),
) -> Response:
    """Download one shared document.

    The database enforces that the document belongs to this client, is marked
    visible, and passed the virus scan; anything else is a 404 so the portal
    cannot be used to probe for document ids.
    """
    document = await service.document(ctx.db, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    filename = str(document.get("filename") or "document.pdf").replace('"', "")
    return Response(
        content=document["content"],
        media_type=str(document.get("mime_type") or "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
