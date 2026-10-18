"""Platform endpoints: public runtime config, Prometheus scrape and API docs."""

from __future__ import annotations

import hmac
from io import BytesIO
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy import text

from app.api.deps import RequestContext, require_permission
from app.db import purpose_transaction
from app.domain.schemas import PlatformBrandRequest
from app.observability.metrics import render_latest
from app.security.captcha import CaptchaVerifier
from app.security.tokens import csrf_required

router = APIRouter(tags=["platform"])


async def _public_platform_brand(request: Request) -> dict[str, str]:
    brand = {"platform_name": "Insurance", "logo_url": "/insurance-logo.svg"}
    factory = getattr(request.app.state, "factory", None)
    if factory is not None:
        async with purpose_transaction(factory, "guest") as db:
            result = await db.execute(text("SELECT * FROM app_security.public_platform_brand()"))
            row = result.one_or_none()
            if row is not None:
                brand = {"platform_name": row.platform_name, "logo_url": row.logo_url}
    return brand


@router.get("/api/public/config")
async def public_config(request: Request, response: Response) -> dict[str, Any]:
    """Non-secret settings the SPA needs before sign-in (CAPTCHA site key, policy)."""
    response.headers["Cache-Control"] = "no-store"
    verifier: CaptchaVerifier | None = getattr(request.app.state, "captcha", None)
    settings = getattr(request.app.state, "settings", None)
    return {
        "captcha": verifier.public_config() if verifier is not None else None,
        "privacy_policy_version": getattr(settings, "privacy_policy_version", "2026-10"),
        **await _public_platform_brand(request),
    }


@router.get("/api/public/manifest.webmanifest", include_in_schema=False)
async def public_manifest(request: Request) -> JSONResponse:
    """Serve the installable app manifest with the currently configured identity."""
    brand = await _public_platform_brand(request)
    return JSONResponse(
        {
            "name": brand["platform_name"],
            "short_name": brand["platform_name"][:12],
            "description": "منصة إدارة شكاوى التأمين",
            "lang": "ar",
            "dir": "rtl",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#081a2f",
            "theme_color": "#081a2f",
            "icons": [
                {
                    "src": brand["logo_url"],
                    "sizes": "any",
                    "purpose": "any maskable",
                }
            ],
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/public/brand/logo", include_in_schema=False)
async def public_brand_logo(request: Request) -> Response:
    factory = getattr(request.app.state, "factory", None)
    if factory is None:
        raise HTTPException(status_code=404, detail="Brand logo not found")
    async with purpose_transaction(factory, "guest") as db:
        result = await db.execute(text("SELECT * FROM app_security.public_platform_logo()"))
        logo = result.one_or_none()
    if logo is None:
        raise HTTPException(status_code=404, detail="Brand logo not found")
    return Response(
        content=logo.logo_content,
        media_type=logo.logo_mime_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.put("/api/platform/brand")
async def update_brand(
    item: PlatformBrandRequest,
    ctx: RequestContext = Depends(require_permission("settings.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, str]:
    updated = await ctx.db.scalar(
        text("SELECT app_security.update_platform_brand(:name, :logo)"),
        {"name": item.platform_name, "logo": item.logo_url},
    )
    if not updated:
        raise HTTPException(status_code=403, detail="Platform Super Admin access required")
    return {"platform_name": item.platform_name.strip(), "logo_url": item.logo_url}


@router.post("/api/platform/brand/logo")
async def upload_platform_logo(
    request: Request,
    ctx: RequestContext = Depends(require_permission("settings.manage")),
    _: None = Depends(csrf_required),
) -> dict[str, str]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 2_097_152:
            raise HTTPException(status_code=413, detail="Logo must be no larger than 2 MB")
        chunks.append(chunk)
    content = b"".join(chunks)
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise HTTPException(status_code=415, detail="Upload a PNG, JPEG or WebP logo")
    try:
        with Image.open(BytesIO(content)) as image:
            if (
                image.format
                != {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[mime]
            ):
                raise HTTPException(
                    status_code=415, detail="Logo content does not match its image type"
                )
            if image.width > 4096 or image.height > 4096 or getattr(image, "n_frames", 1) != 1:
                raise HTTPException(
                    status_code=413, detail="Logo dimensions or animation are not supported"
                )
            image.verify()
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=415, detail="The uploaded image is invalid") from exc
    updated = await ctx.db.scalar(
        text("SELECT app_security.update_platform_logo(:mime, :content)"),
        {"mime": mime, "content": content},
    )
    if not updated:
        raise HTTPException(status_code=403, detail="Platform Super Admin access required")
    return {"logo_url": "/api/public/brand/logo"}


@router.get("/api/staff/customers")
async def customer_directory(
    ctx: RequestContext = Depends(require_permission("users.view")),
) -> list[dict[str, Any]]:
    if (
        not ctx.scope.all_companies
        or ctx.scope.company_ids
        or ctx.scope.insurance_types
        or ctx.scope.ownership != "all"
    ):
        raise HTTPException(status_code=403, detail="Global administrator scope required")
    result = await ctx.db.scalar(text("SELECT app_security.customer_directory()"))
    if isinstance(result, str):
        import json

        result = json.loads(result)
    return result or []


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Prometheus scrape protected exclusively by the configured bearer token."""
    settings = getattr(request.app.state, "settings", None)
    token: str | None = getattr(settings, "metrics_token", None)
    supplied = request.headers.get("authorization", "")
    allowed = bool(token and hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()))
    if not allowed:
        raise HTTPException(status_code=404, detail="Not Found")
    body, content_type = render_latest()
    return Response(body, media_type=content_type, headers={"Cache-Control": "no-store"})


def _docs_enabled(request: Request) -> bool:
    settings = getattr(request.app.state, "settings", None)
    # Fail closed when startup has not installed settings. There is deliberately
    # no production override: docs are never served in a production environment.
    return settings is not None and not settings.is_production


@router.get("/openapi.json", include_in_schema=False)
async def openapi_json(request: Request) -> JSONResponse:
    if not _docs_enabled(request):
        raise HTTPException(status_code=404, detail="Not Found")
    return JSONResponse(request.app.openapi())


@router.get("/docs/", include_in_schema=False)
@router.get("/docs", include_in_schema=False)
async def swagger(request: Request) -> HTMLResponse:
    if not _docs_enabled(request):
        raise HTTPException(status_code=404, detail="Not Found")
    return get_swagger_ui_html(openapi_url="/openapi.json", title=request.app.title)


@router.get("/redoc/", include_in_schema=False)
@router.get("/redoc", include_in_schema=False)
async def redoc(request: Request) -> HTMLResponse:
    if not _docs_enabled(request):
        raise HTTPException(status_code=404, detail="Not Found")
    return get_redoc_html(openapi_url="/openapi.json", title=request.app.title)
