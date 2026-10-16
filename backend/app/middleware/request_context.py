"""Request ID, trusted client IP, structured access log and Prometheus metrics."""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.middleware.policy import policy_for, route_template
from app.observability import metrics
from app.observability.logging import (
    actor_id_var,
    actor_kind_var,
    client_ip_var,
    request_id_var,
    route_var,
)

access_logger = logging.getLogger("app.access")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")
_QUIET = frozenset({"/api/health/live", "/api/health/ready", "/metrics"})


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        policy = policy_for(scope)
        headers = Headers(scope=scope)
        incoming = headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid4().hex
        peer = scope.get("client")
        client_ip = policy.resolver.resolve(
            peer[0] if peer else None, headers.get("x-forwarded-for")
        )
        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["client_ip"] = client_ip
        tokens = (
            request_id_var.set(request_id),
            client_ip_var.set(client_ip),
            actor_id_var.set(None),
            actor_kind_var.set(None),
            route_var.set(None),
        )
        try:
            if scope["type"] == "websocket":
                await self.app(scope, receive, send)
                return
            await self._http(scope, receive, send, request_id)
        finally:
            for var, token in zip(
                (request_id_var, client_ip_var, actor_id_var, actor_kind_var, route_var),
                tokens,
                strict=True,
            ):
                var.reset(token)

    async def _http(self, scope: Scope, receive: Receive, send: Send, request_id: str) -> None:
        method = scope["method"]
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        in_progress = metrics.HTTP_IN_PROGRESS.labels(method=method)
        in_progress.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            in_progress.dec()
            elapsed = time.perf_counter() - started
            route = route_template(scope)
            route_var.set(route)
            metrics.HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
            metrics.HTTP_LATENCY.labels(method=method, route=route).observe(elapsed)
            state = scope.get("state", {})
            if state.get("actor_id"):
                actor_id_var.set(str(state["actor_id"]))
                actor_kind_var.set(str(state.get("actor_kind", "")))
            path = scope.get("path", "")
            level = logging.DEBUG if path in _QUIET and status < 400 else logging.INFO
            if status >= 500:
                level = logging.ERROR
            access_logger.log(
                level,
                "%s %s %s",
                method,
                route,
                status,
                extra={
                    "event": "http.access",
                    "method": method,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
