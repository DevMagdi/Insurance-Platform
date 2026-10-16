"""Edge rate limiting per IP / user / API key / endpoint class, plus abuse detection.

Routes keep their own precise, business-level limits (OTP per mobile, complaint
submission per customer, ...). This middleware adds a uniform outer layer that
runs before any database work:

* a global per-IP and per-user ceiling for every ``/api`` request;
* strict buckets for expensive or sensitive endpoint classes (authentication,
  uploads, exports, AI/voice/e-mail actions, privacy exports, webhooks);
* an **abuse detector**: an identity producing a burst of 401/403/404 responses
  (typical of IDOR probing / ID enumeration) is blocked for 15 minutes and a
  ``security.suspicious_access`` event + metric is emitted for alerting.

The middleware is inert until ``app.state.rate_limiter`` is installed by the
application lifespan, which keeps unit tests independent of Redis.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.middleware.policy import HttpPolicy, policy_for, route_template
from app.observability import metrics
from app.security.rate_limit import LimitDecision, RateLimiter

security_logger = logging.getLogger("app.security")


@dataclass(frozen=True)
class Limit:
    identity: str  # "ip" | "user" | "key"
    limit: int
    seconds: int


@dataclass(frozen=True)
class EndpointClass:
    name: str
    pattern: re.Pattern[str]
    methods: frozenset[str] | None
    limits: tuple[Limit, ...]

    def matches(self, method: str, path: str) -> bool:
        return (self.methods is None or method in self.methods) and bool(self.pattern.match(path))


def _cls(name: str, pattern: str, methods: str | None, *limits: Limit) -> EndpointClass:
    return EndpointClass(
        name,
        re.compile(pattern),
        frozenset(methods.split(",")) if methods else None,
        limits,
    )


# First match wins; the global ceiling below always applies in addition.
ENDPOINT_CLASSES: tuple[EndpointClass, ...] = (
    _cls("auth", r"^/api/auth/(customer|staff)/", "POST", Limit("ip", 60, 300)),
    _cls("auth_refresh", r"^/api/auth/refresh$", "POST", Limit("ip", 120, 300)),
    _cls("guest_track", r"^/api/guest/", "POST", Limit("ip", 30, 600)),
    _cls(
        "complaint_submit",
        r"^/api/customer/complaints$",
        "POST",
        Limit("user", 20, 3600),
        Limit("ip", 100, 3600),
    ),
    _cls(
        "upload",
        r"^/api/(customer|staff)/(attachments/uploads/[^/]+/content|complaints/[^/]+/attachments/plan)$",
        "POST,PUT",
        Limit("user", 60, 3600),
        Limit("ip", 200, 3600),
    ),
    _cls(
        "export",
        r"^/api/staff/(exports|complaints/export|audit/[^/]+\.(csv|xlsx)$|reports/schedules/[^/]+/run$)",
        None,
        Limit("user", 30, 600),
    ),
    _cls(
        "pii_reveal",
        r"^/api/staff/complaints/[^/]+/national-id:reveal$",
        "POST",
        Limit("user", 30, 3600),
    ),
    _cls(
        "ai_call",
        r"^/api/staff/complaints/[^/]+/(ai/analyze|ai/insights/[^/]+/send|voice-followups"
        r"|email|email/reply|whatsapp)$",
        "POST",
        Limit("user", 60, 600),
        Limit("user", 600, 86_400),
    ),
    _cls("privacy_export", r"^/api/customer/privacy/export$", None, Limit("user", 5, 3600)),
    _cls("privacy_request", r"^/api/customer/privacy/requests$", "POST", Limit("user", 5, 86_400)),
    _cls("webhook", r"^/api/webhooks/", None, Limit("ip", 600, 60)),
    _cls("public_api", r"^/api/public/v1/", None, Limit("key", 300, 60), Limit("ip", 900, 60)),
)
GLOBAL_LIMITS = (Limit("ip", 1_200, 60), Limit("user", 600, 60))
EXEMPT = re.compile(r"^/api/health/|^/metrics$")
ABUSE_WINDOW = 300
ABUSE_BLOCK = 900
_DENIED = frozenset({401, 403, 404})


def _bearer_key(headers: Headers) -> str | None:
    value = headers.get("authorization", "")
    if value.lower().startswith("bearer ") and len(value) > 7:
        return hashlib.sha256(value[7:].strip().encode()).hexdigest()
    return None


def _user(scope: Scope, cookie_header: str | None) -> str | None:
    """Verified JWT subject from the access cookie (never trusted when invalid)."""
    if not cookie_header or "access_token=" not in cookie_header:
        return None
    auth = getattr(getattr(scope.get("app"), "state", None), "auth", None)
    manager = getattr(auth, "tokens", None)
    if manager is None:
        return None
    from http.cookies import CookieError, SimpleCookie

    try:
        cookie: SimpleCookie = SimpleCookie(cookie_header)
    except CookieError:
        return None
    morsel = cookie.get("access_token")
    if morsel is None:
        return None
    try:
        claims = manager.parse(morsel.value, "access")
    except Exception:
        return None
    return f"{claims.subject.scope}:{claims.subject.id}"


async def _too_many(send: Send, decision: LimitDecision, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
    ]
    headers += [(k.lower().encode(), v.encode()) for k, v in decision.headers().items()]
    await send({"type": "http.response.start", "status": 429, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        state = getattr(scope.get("app"), "state", None)
        limiter: RateLimiter | None = getattr(state, "rate_limiter", None)
        if limiter is None or not path.startswith("/api/") or EXEMPT.match(path):
            await self.app(scope, receive, send)
            return
        policy = policy_for(scope)
        headers = Headers(scope=scope)
        method: str = scope["method"]
        ip = str(scope.get("state", {}).get("client_ip") or "0.0.0.0")  # noqa: S104
        user = _user(scope, headers.get("cookie"))
        identities = {"ip": f"ip:{ip}", "user": user, "key": _bearer_key(headers)}

        blocked = await self._blocked(limiter, user or f"ip:{ip}")
        if blocked:
            await _too_many(
                send, LimitDecision(False, 0, 0, blocked), "Temporarily blocked; try later"
            )
            return

        endpoint = next((c for c in ENDPOINT_CLASSES if c.matches(method, path)), None)
        checks: list[tuple[str, Limit]] = [("global", limit) for limit in GLOBAL_LIMITS]
        if endpoint is not None:
            checks += [(endpoint.name, limit) for limit in endpoint.limits]
        tightest: LimitDecision | None = None
        for category, limit in checks:
            identity = identities.get(limit.identity)
            if identity is None:
                continue
            decision = await limiter.hit(
                f"edge:{category}:{limit.identity}:{limit.seconds}",
                identity,
                limit.limit,
                limit.seconds,
            )
            if not decision.allowed:
                await _too_many(send, decision, "Too many requests; slow down")
                return
            if category != "global" and (
                tightest is None or decision.remaining < tightest.remaining
            ):
                tightest = decision

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = int(message["status"])
                if tightest is not None:
                    response_headers = MutableHeaders(scope=message)
                    for key, value in tightest.headers().items():
                        response_headers.setdefault(key, value)
                if status in _DENIED:
                    await self._record_denied(
                        limiter, policy, scope, status, user, identities["ip"] or ""
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    async def _blocked(limiter: RateLimiter, identity: str) -> int:
        ttl = await limiter.redis.ttl(limiter.key_for("abuse:block", identity))
        return int(ttl) if ttl and int(ttl) > 0 else 0

    @staticmethod
    async def _record_denied(
        limiter: RateLimiter,
        policy: HttpPolicy,
        scope: Scope,
        status: int,
        user: str | None,
        ip_identity: str,
    ) -> None:
        actor_kind = user.split(":", 1)[0] if user else "anonymous"
        metrics.ACCESS_DENIED.labels(status=str(status), actor_kind=actor_kind).inc()
        # Unauthenticated 401s are expected (expired sessions); count the rest.
        if user is None and status == 401:
            return
        identity = user or ip_identity
        threshold = policy.abuse_denied_threshold * (1 if user else 2)
        decision = await limiter.hit("abuse:denied", identity, threshold, ABUSE_WINDOW)
        if decision.allowed:
            return
        block_key = limiter.key_for("abuse:block", identity)
        if not await limiter.redis.set(block_key, "1", ex=ABUSE_BLOCK, nx=True):
            return
        metrics.SUSPICIOUS_ACCESS.labels(reason="denied_burst").inc()
        details: dict[str, Any] = {
            "event": "security.suspicious_access",
            "reason": "denied_burst",
            "subject": user,
            "route": route_template(scope),
            "last_status": status,
            "window_seconds": ABUSE_WINDOW,
            "block_seconds": ABUSE_BLOCK,
        }
        security_logger.warning("security.suspicious_access", extra=details)
