"""Request body ceilings enforced before parsing (DoS / memory exhaustion).

``Content-Length`` is checked up front; chunked or lying clients are cut off
while streaming. Attachment uploads get the per-file ceiling; everything else
is JSON and is capped at ``MAX_JSON_BODY_BYTES``.
"""

from __future__ import annotations

import json
import re

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.middleware.policy import policy_for
from app.observability import metrics

_UPLOAD = re.compile(r"^/api/(customer|staff)/attachments/uploads/[^/]+/content$")
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class _BodyTooLarge(Exception):
    pass


async def _too_large(send: Send) -> None:
    body = json.dumps({"detail": "Request body too large"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        policy = policy_for(scope)
        limit = policy.max_upload_body if _UPLOAD.match(scope["path"]) else policy.max_json_body
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdecimal() or int(declared) > limit):
            metrics.REQUEST_BODY_REJECTED.labels(reason="content_length").inc()
            await _too_large(send)
            return
        received = 0
        started = False
        overflow = False

        async def limited_receive() -> Message:
            nonlocal received, overflow
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    overflow = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            # Frameworks may convert the receive error into their own 4xx (FastAPI
            # answers 400 "error parsing the body"); the real cause is the size.
            nonlocal started
            if overflow:
                if message["type"] == "http.response.start" and not started:
                    started = True
                    await _too_large(send)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            if not started:
                await _too_large(send)
        if overflow:
            metrics.REQUEST_BODY_REJECTED.labels(reason="stream").inc()
