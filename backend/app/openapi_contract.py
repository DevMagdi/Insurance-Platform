"""Document cookie/CSRF/permission contracts that FastAPI cannot infer automatically."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api import analytics, attachments, audit, auth, catalog, customer, exports, staff, workflow
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
    "/api/auth/staff/password/change",
    "/api/auth/staff/2fa/verify",
    "/api/auth/staff/2fa/enroll/start",
    "/api/auth/staff/2fa/enroll/confirm",
}


def dependencies(root: Dependant) -> Iterator[object]:
    for dependency in root.dependencies:
        yield dependency.call
        yield from dependencies(dependency)


def decorate_spec(app: FastAPI, spec: dict[str, Any]) -> dict[str, Any]:
    """Add accurate cookies, CSRF, permissions and WebSocket notes to live OpenAPI."""
    spec["info"]["description"] = (
        "Implemented Phase 2 and Phase 3 HTTP routes. Authenticate with an HttpOnly access_token "
        "cookie; /api/auth/refresh uses the HttpOnly refresh_token cookie. "
        "All protected writes, including refresh, need X-CSRF-Token matching "
        "the readable csrf_token cookie. Authorization, AccessScope and "
        "PostgreSQL RLS are enforced at runtime. Future routes are documented "
        "separately in openapi-roadmap.yaml."
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
    ]
    for route in routes:
        if not isinstance(route, APIRoute):
            continue
        calls = tuple(dependencies(route.dependant))
        for method in route.methods or ():
            operation = spec["paths"][route.path][method.lower()]
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
