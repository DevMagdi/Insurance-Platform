"""Platform endpoints: public runtime config, Prometheus scrape and API docs."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from app.observability.metrics import render_latest
from app.security.captcha import CaptchaVerifier

router = APIRouter(tags=["platform"])


@router.get("/api/public/config")
async def public_config(request: Request, response: Response) -> dict[str, Any]:
    """Non-secret settings the SPA needs before sign-in (CAPTCHA site key, policy)."""
    response.headers["Cache-Control"] = "public, max-age=300"
    verifier: CaptchaVerifier | None = getattr(request.app.state, "captcha", None)
    settings = getattr(request.app.state, "settings", None)
    return {
        "captcha": verifier.public_config() if verifier is not None else None,
        "privacy_policy_version": getattr(settings, "privacy_policy_version", "2026-10"),
    }


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
