"""Admin-managed encrypted integrations, outgoing webhooks, API keys and DLQ."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import RequestContext, integration_service, require_permission
from app.domain.schemas import (
    ApiKeyCreate,
    ConnectionTestRequest,
    IntegrationConfigSpec,
    OAuthStartRequest,
    SandboxSendRequest,
    WebhookCreate,
)
from app.security.tokens import csrf_required

router = APIRouter(prefix="/api/staff/admin", tags=["integration-admin"])


@router.get("/integration-connections")
async def integrations(
    request: Request,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
) -> list[dict[str, Any]]:
    """Stored configurations: provider, scope, enablement and connection status.

    Credential values are never included; only ``has_credentials`` is returned.
    """
    return await integration_service(request).list_configs(ctx.db)


@router.post("/integration-connections/test")
async def test_integration_connection(
    item: ConnectionTestRequest,
    request: Request,
    response: Response,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    """Non-mutating credential probe; records the status and last test time."""
    result = await integration_service(request).test_connection(
        ctx.db, scope=ctx.scope, company_id=item.company_id, provider=item.provider
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/integration-connections/sandbox-test", status_code=202)
async def sandbox_integration_send(
    item: SandboxSendRequest,
    request: Request,
    response: Response,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    """Send the fixed sandbox template to the stored sandbox address only."""
    result = await integration_service(request).sandbox_send(ctx.db, scope=ctx.scope, request=item)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/integration-connections/oauth/{provider}/authorize")
async def oauth_authorize(
    provider: str,
    item: OAuthStartRequest,
    request: Request,
    response: Response,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    if item.provider != provider:
        raise HTTPException(status_code=422, detail="OAuth provider mismatch")
    result = await integration_service(request).oauth_start(ctx.db, scope=ctx.scope, request=item)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/integration-connections/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=2048),
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
) -> RedirectResponse:
    """Browser return leg: validates the signed state, stores tokens and redirects."""
    if not code or not state:
        raise HTTPException(status_code=400, detail="OAuth code and state are required")
    redirect, _ = await integration_service(request).oauth_callback(
        ctx.db, scope=ctx.scope, provider=provider, code=code, state=state
    )
    return RedirectResponse(url=redirect, status_code=303)


@router.put("/integration-connections")
async def configure_integration(
    item: IntegrationConfigSpec,
    request: Request,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    return await integration_service(request).save_config(
        ctx.db,
        company_id=item.company_id,
        provider=item.provider,
        credentials=item.credentials,
        enabled=item.enabled,
        clear_credentials=item.clear_credentials,
    )


@router.get("/webhooks")
async def webhooks(
    request: Request,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
) -> list[dict[str, Any]]:
    return await integration_service(request).list_webhooks(ctx.db)


@router.post("/webhooks", status_code=201)
async def create_webhook(
    item: WebhookCreate,
    request: Request,
    response: Response,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    result = await integration_service(request).create_webhook(
        ctx.db,
        company_id=item.company_id,
        url=item.url,
        event_types=set(item.event_types),
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def deactivate_webhook(
    webhook_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> None:
    await integration_service(request).deactivate_webhook(ctx.db, webhook_id)


@router.get("/api-keys")
async def api_keys(
    request: Request,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
) -> list[dict[str, Any]]:
    return await integration_service(request).list_api_keys(ctx.db)


@router.post("/api-keys", status_code=201)
async def create_api_key(
    item: ApiKeyCreate,
    request: Request,
    response: Response,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    result = await integration_service(request).create_api_key(
        ctx.db,
        permissions=item.permissions,
        all_companies=item.all_companies,
        company_ids=item.company_ids,
        expires_at=item.expires_at,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> None:
    if not await integration_service(request).revoke_api_key(ctx.db, key_id):
        raise HTTPException(status_code=404, detail="API key unavailable")


@router.get("/integration-jobs/dead-letters")
async def integration_dead_letters(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
) -> list[dict[str, Any]]:
    return await integration_service(request).dead_letters(ctx.db, limit)


@router.post("/integration-jobs/dead-letters/{job_id}/retry", status_code=202)
async def retry_dead_letter(
    job_id: UUID,
    request: Request,
    ctx: RequestContext = Depends(require_permission("integrations.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, bool]:
    await integration_service(request).requeue_dead_letter(ctx.db, job_id)
    return {"queued": True}
