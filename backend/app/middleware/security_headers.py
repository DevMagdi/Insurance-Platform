"""Defensive response headers (OWASP A05) and a no-store default for API data."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.middleware.policy import policy_for

# JSON APIs never render HTML, so the strictest policy applies.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# Swagger UI/ReDoc (non-production only) load pinned assets from jsDelivr.
DOCS_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: "
    "https://fastapi.tiangolo.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)
PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
    "microphone=(), payment=(), usb=(), interest-cohort=()"
)
_DOCS = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        policy = policy_for(scope)
        path: str = scope.get("path", "")
        is_docs = path.startswith(_DOCS)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
                headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
                headers.setdefault("Content-Security-Policy", DOCS_CSP if is_docs else API_CSP)
                headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
                if policy.hsts_seconds:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        f"max-age={policy.hsts_seconds}; includeSubDomains",
                    )
                if path.startswith("/api/") and "cache-control" not in headers:
                    # Personal data must never land in shared or browser caches by default.
                    headers["Cache-Control"] = "no-store"
                    headers.setdefault("Pragma", "no-cache")
                if "server" in headers:
                    del headers["server"]
            await send(message)

        await self.app(scope, receive, send_wrapper)
