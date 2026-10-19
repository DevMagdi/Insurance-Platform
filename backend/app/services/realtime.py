"""Customer ticket realtime updates over Server-Sent Events.

Each poll opens a fresh scoped transaction and re-runs the full session,
token-version and ownership checks (the same discipline as the staff
WebSocket), so revocation or reassignment stops the stream on the next
tick instead of leaking a stale subscription. The stream only ever emits
the caller's own ticket status and public timeline size — never message
bodies — and clients refetch the detail through the normal scoped API.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import load_context
from app.db import Actor, scoped_transaction
from app.repositories.complaints import ComplaintRepository
from app.security.tokens import TokenClaims
from app.services.complaints import ComplaintService


def complaint_signature(
    detail: dict[str, Any], timeline: dict[str, Any], attachments: Any = ()
) -> str:
    """Stable fingerprint of everything the ticket screen renders."""
    last_event = timeline["events"][-1]["id"] if timeline.get("events") else None
    last_message = timeline["messages"][-1]["id"] if timeline.get("messages") else None
    raw = json.dumps(
        {
            "status": detail.get("status"),
            "updated_at": str(detail.get("updated_at")),
            "assigned_agent_id": str(detail.get("assigned_agent_id")),
            "events": len(timeline.get("events", [])),
            "messages": len(timeline.get("messages", [])),
            "last_event": str(last_event),
            "last_message": str(last_message),
            "attachments": [
                [str(item.id), str(item.scan_status)] for item in attachments or ()
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(jsonable_encoder(payload))}\n\n"


async def stream_customer_complaint(
    *,
    factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    complaints: ComplaintService,
    claims: TokenClaims,
    complaint_id: UUID,
    poll_seconds: float = 3.0,
    max_polls: int | None = None,
    is_disconnected: Callable[[], Any] | None = None,
) -> AsyncIterator[str]:
    """Yield an initial snapshot, then an ``update`` event per visible change."""
    if claims.subject.scope != "customer":
        raise HTTPException(status_code=403, detail="Customer account required")
    actor = Actor(claims.subject.id, "customer")
    previous: str | None = None
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        if is_disconnected is not None and await _disconnected(is_disconnected):
            return
        async with scoped_transaction(factory, actor) as db:
            ctx = await load_context(db, claims, redis, complaints)
            detail = await complaints.detail(db, ctx.scope, complaint_id)
            parent = await complaints.parent(db, ctx.scope, complaint_id)
            timeline = await ComplaintRepository().timeline(db, ctx.scope, parent, 1, 100)
        signature = complaint_signature(detail, timeline)
        payload = {
            "complaint_id": str(complaint_id),
            "ticket_no": detail.get("ticket_no"),
            "status": detail.get("status"),
            "signature": signature,
        }
        if previous is None:
            yield sse_event("snapshot", payload)
        elif signature != previous:
            yield sse_event("update", payload)
        else:
            yield ": heartbeat\n\n"
        previous = signature
        if max_polls is not None and polls >= max_polls:
            return
        await asyncio.sleep(poll_seconds)


async def stream_staff_complaint(
    *,
    factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    complaints: ComplaintService,
    claims: TokenClaims,
    complaint_id: UUID,
    poll_seconds: float = 3.0,
    max_polls: int | None = None,
    is_disconnected: Callable[[], Any] | None = None,
) -> AsyncIterator[str]:
    """Staff twin of the customer stream: same per-poll revalidation.

    Every tick re-runs session, token-version, ``complaints.view``
    permission and AccessScope checks, and the signature also covers
    attachment scan states so a newly-clean file refreshes the open ticket.
    Only status + signature are emitted — never message bodies.
    """
    if claims.subject.scope != "staff":
        raise HTTPException(status_code=403, detail="Staff account required")
    actor = Actor(claims.subject.id, "staff")
    previous: str | None = None
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        if is_disconnected is not None and await _disconnected(is_disconnected):
            return
        async with scoped_transaction(factory, actor) as db:
            ctx = await load_context(db, claims, redis, complaints)
            if "complaints.view" not in ctx.permissions:
                raise HTTPException(status_code=403, detail="Permission denied")
            detail = await complaints.detail(db, ctx.scope, complaint_id)
            parent = await complaints.parent(db, ctx.scope, complaint_id)
            timeline = await ComplaintRepository().timeline(db, ctx.scope, parent, 1, 100)
            attachments = await ComplaintRepository().attachments(db, ctx.scope, parent)
        signature = complaint_signature(detail, timeline, attachments)
        payload = {
            "complaint_id": str(complaint_id),
            "ticket_no": detail.get("ticket_no"),
            "status": detail.get("status"),
            "signature": signature,
        }
        if previous is None:
            yield sse_event("snapshot", payload)
        elif signature != previous:
            yield sse_event("update", payload)
        else:
            yield ": heartbeat\n\n"
        previous = signature
        if max_polls is not None and polls >= max_polls:
            return
        await asyncio.sleep(poll_seconds)


async def _disconnected(check: Callable[[], Any]) -> bool:
    result = check()
    if asyncio.iscoroutine(result):
        result = await result
    return bool(result)
