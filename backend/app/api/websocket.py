"""Staff complaint updates: same scope, permissions and session rechecked every poll."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import load_context, require_permission
from app.db import scoped_transaction
from app.domain.schemas import ComplaintFilter

router = APIRouter(prefix="/api/staff", tags=["staff"])
_view_permission = require_permission("complaints.view")


def same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin", "")
    host = websocket.headers.get("host", "")
    try:
        parsed = urlsplit(origin)
    except ValueError:  # Malformed IPv6/ports are not a trusted Origin.
        return False
    return bool(
        parsed.scheme in ("http", "https")
        and parsed.netloc
        and parsed.netloc.lower() == host.lower()
        and not parsed.username
        and not parsed.password
    )


@router.websocket("/complaints/stream")
async def complaints_stream(websocket: WebSocket) -> None:
    if not same_origin(websocket):
        await websocket.close(code=1008, reason="Origin not permitted")
        return
    auth = websocket.app.state.auth
    try:
        claims = auth.tokens.parse(websocket.cookies.get("access_token"), "access")
        filters = ComplaintFilter.model_validate(dict(websocket.query_params))
        factory: async_sessionmaker[AsyncSession] = websocket.app.state.factory
        async with scoped_transaction(factory, claims.subject) as db:
            ctx = await load_context(
                db, claims, websocket.app.state.redis, websocket.app.state.complaints
            )
            _view_permission.verify(ctx)
            initial = await ctx.complaints.list(db, ctx.scope, filters)
        await websocket.accept()
        await websocket.send_json(jsonable_encoder({"type": "snapshot", **initial}))
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=15)
                if message != "refresh":
                    await websocket.close(code=1008, reason="Unsupported message")
                    return
                await auth.limits.check("ws:session", str(claims.session_id), 60, 60)
            except TimeoutError:
                message = "heartbeat"
            async with scoped_transaction(factory, claims.subject) as db:
                ctx = await load_context(
                    db, claims, websocket.app.state.redis, websocket.app.state.complaints
                )
                _view_permission.verify(ctx)
                payload = await ctx.complaints.list(db, ctx.scope, filters)
            await websocket.send_json(
                jsonable_encoder(
                    {"type": "snapshot" if message == "refresh" else "heartbeat", **payload}
                )
            )
    except (HTTPException, ValidationError):
        await websocket.close(code=1008, reason="Authentication or scope changed")
    except WebSocketDisconnect:
        return
