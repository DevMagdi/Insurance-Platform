"""Document cookie/CSRF/permission contracts that FastAPI cannot infer automatically."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api import (
    admin_integrations,
    analytics,
    attachments,
    audit,
    auth,
    catalog,
    customer,
    exports,
    integration_webhooks,
    integrations,
    operations,
    platform,
    privacy,
    public_api,
    reports,
    staff,
    storage_admin,
    workflow,
)
from app.api.deps import PermissionGate
from app.security.tokens import csrf_required

PUBLIC = {
    "/api/health/live",
    "/api/health/ready",
    "/api/companies",
    "/api/guest/track",
    "/api/auth/customer/otp/start",
    "/api/auth/customer/otp/verify",
    "/api/auth/staff/login",
    "/api/auth/staff/invitation/accept",
    "/api/auth/staff/invitation/inspect",
    "/api/auth/staff/password/change",
    "/api/auth/staff/2fa/verify",
    "/api/auth/staff/sms-otp/verify",
    "/api/auth/staff/2fa/enroll/start",
    "/api/auth/staff/2fa/enroll/confirm",
    "/api/public/config",
}


def dependencies(root: Dependant) -> Iterator[object]:
    for dependency in root.dependencies:
        yield dependency.call
        yield from dependencies(dependency)


def decorate_spec(app: FastAPI, spec: dict[str, Any]) -> dict[str, Any]:
    """Add accurate cookies, CSRF, permissions and WebSocket notes to live OpenAPI."""
    spec["info"]["description"] = (
        "Implemented HTTP routes across product Phases 1-9. Authenticate with an HttpOnly "
        "access_token cookie; /api/auth/refresh uses the HttpOnly refresh_token cookie. "
        "All protected writes, including refresh, need X-CSRF-Token matching the readable "
        "csrf_token cookie. Authorization, AccessScope and PostgreSQL RLS are enforced at "
        "runtime. Every /api route is rate limited per IP, user, API key and endpoint class; "
        "a 429 response carries Retry-After and X-RateLimit-* headers. Public forms "
        "(OTP start, guest tracking, staff login, invitation acceptance) require a "
        "Cloudflare Turnstile token in X-Captcha-Token when CAPTCHA is configured "
        "(see /api/public/config). Future routes are documented separately in "
        "openapi-roadmap.yaml."
    )
    spec["components"].setdefault("securitySchemes", {}).update(
        {
            "AccessCookie": {
                "type": "apiKey",
                "in": "cookie",
                "name": "access_token",
                "description": "Short-lived JWT; Secure, HttpOnly, SameSite=Lax, Path=/api.",
            },
            "RefreshCookie": {
                "type": "apiKey",
                "in": "cookie",
                "name": "refresh_token",
                "description": "Rotating JWT; Secure, HttpOnly, SameSite=Lax, Path=/api/auth.",
            },
        }
    )
    # Recent FastAPI versions include routers lazily (_IncludedRouter), so
    # app.routes contains only health routes; inspect each declared router.
    routes = [
        *app.routes,
        *auth.router.routes,
        *customer.router.routes,
        *staff.router.routes,
        *workflow.router.routes,
        *analytics.router.routes,
        *audit.router.routes,
        *catalog.router.routes,
        *exports.router.routes,
        *attachments.router.routes,
        *integrations.router.routes,
        *admin_integrations.router.routes,
        *integration_webhooks.router.routes,
        *public_api.router.routes,
        *reports.router.routes,
        *operations.router.routes,
        *privacy.router.routes,
        *storage_admin.router.routes,
        *platform.router.routes,
    ]
    for route in routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        calls = tuple(dependencies(route.dependant))
        for method in route.methods or ():
            operation = spec["paths"][route.path][method.lower()]
            if route.path == "/api/platform/brand/logo" and method == "POST":
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        mime: {"schema": {"type": "string", "format": "binary"}}
                        for mime in ("image/png", "image/jpeg", "image/webp")
                    },
                }
            if route.path == "/api/auth/refresh":
                operation["security"] = [{"RefreshCookie": []}]
            else:
                operation["security"] = [] if route.path in PUBLIC else [{"AccessCookie": []}]
            permission = next(
                (dep.code for dep in calls if isinstance(dep, PermissionGate)),
                None,
            )
            if permission is not None:
                operation["x-required-permission"] = permission
            action = next(
                (
                    getattr(dep, "captcha_action", None)
                    for dep in calls
                    if hasattr(dep, "captcha_action")
                ),
                None,
            )
            if action is not None:
                operation["x-captcha-action"] = action
                operation.setdefault("parameters", []).append(
                    {
                        "name": "X-Captcha-Token",
                        "in": "header",
                        "required": False,
                        "description": (
                            "Cloudflare Turnstile token; mandatory whenever "
                            "/api/public/config advertises a captcha site key."
                        ),
                        "schema": {"type": "string", "maxLength": 2048},
                    }
                )
            if csrf_required in calls or route.path == "/api/auth/refresh":
                operation.setdefault("parameters", []).append(
                    {
                        "name": "X-CSRF-Token",
                        "in": "header",
                        "required": True,
                        "description": "Must equal the csrf_token cookie (double-submit).",
                        "schema": {"type": "string"},
                    }
                )
    spec["x-websockets"] = [
        {
            "path": "/api/staff/complaints/stream",
            "description": (
                "Same-origin, AccessCookie, complaints.view; filtered scoped snapshots "
                "on connect, refresh or heartbeat."
            ),
        }
    ]
    spec["x-event-streams"] = [
        {
            "path": "/api/customer/complaints/{complaint_id}/stream",
            "description": (
                "Server-Sent Events; same-origin AccessCookie for the owning customer. "
                "Emits snapshot/update events (status + signature only, never message "
                "bodies) and revalidates session, token version and ownership per poll."
            ),
        }
    ]
    return spec
