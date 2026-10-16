"""CORS allow-list and Origin/Fetch-Metadata CSRF defence in depth (OWASP A01/A05).

The SPA is served same-origin, so by default NO cross-origin access is granted.
Extra origins must be listed in ``CORS_ALLOWED_ORIGINS``. Independently of the
double-submit CSRF token, any state-changing request whose ``Origin`` is neither
our own nor allow-listed, or which the browser labels ``Sec-Fetch-Site:
cross-site``, is rejected before routing (this also blocks login CSRF on the
public authentication endpoints, which cannot carry a CSRF cookie yet).
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.middleware.policy import HttpPolicy, policy_for

UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Server-to-server endpoints authenticate with signatures / API keys, not cookies.
EXEMPT_PREFIXES = ("/api/webhooks/", "/api/public/v1/")
ALLOWED_HEADERS = (
    "Content-Type, X-CSRF-Token, Idempotency-Key, X-Upload-Token, X-Download-Token, "
    "X-Captcha-Token, X-Request-ID"
)
ALLOWED_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"


def _same_origin(origin: str, host: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return bool(
        parsed.scheme in ("http", "https")
        and parsed.netloc
        and host
        and parsed.netloc.lower() == host.lower()
        and not parsed.username
    )


def origin_allowed(origin: str, hosts: tuple[str, ...], policy: HttpPolicy) -> bool:
    return (
        any(_same_origin(origin, host) for host in hosts if host)
        or origin.rstrip("/").lower() in policy.allowed_origins
    )


async def _reject(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class OriginGuardMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return
        policy = policy_for(scope)
        headers = Headers(scope=scope)
        method = scope["method"]
        path = scope["path"]
        origin = headers.get("origin")
        # X-Forwarded-Host cannot be forged cross-site: it is not CORS-safelisted.
        hosts = (headers.get("host", ""), headers.get("x-forwarded-host", "").split(",")[0].strip())
        explicit = bool(origin) and origin.rstrip("/").lower() in policy.allowed_origins  # type: ignore[union-attr]

        if method == "OPTIONS" and origin and headers.get("access-control-request-method"):
            if not explicit:
                await _reject(send, 403, "Origin not allowed")
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        (b"access-control-allow-origin", origin.encode()),
                        (b"access-control-allow-credentials", b"true"),
                        (b"access-control-allow-methods", ALLOWED_METHODS.encode()),
                        (b"access-control-allow-headers", ALLOWED_HEADERS.encode()),
                        (b"access-control-max-age", b"600"),
                        (b"vary", b"Origin"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        if method in UNSAFE and not path.startswith(EXEMPT_PREFIXES):
            if origin and not origin_allowed(origin, hosts, policy):
                await _reject(send, 403, "Cross-origin request blocked")
                return
            if not origin and headers.get("sec-fetch-site") == "cross-site":
                await _reject(send, 403, "Cross-site request blocked")
                return

        if not explicit:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["Access-Control-Allow-Origin"] = origin or ""
                response_headers["Access-Control-Allow-Credentials"] = "true"
                response_headers.append("Vary", "Origin")
            await send(message)

        await self.app(scope, receive, send_wrapper)
