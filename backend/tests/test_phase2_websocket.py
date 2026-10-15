"""WebSockets check origin, permission and scope again on every message."""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import websocket as websocket_api
from app.api.deps import RequestContext
from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.main import app
from app.security.tokens import TokenManager
from app.settings import Settings


@pytest.fixture
def fixture(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="test",
    )
    tokens = TokenManager(settings)
    actor = Actor(uuid4(), "staff")
    sid = uuid4()
    access = tokens.issue(actor, sid, 0, "access", timedelta(minutes=5))
    service = SimpleNamespace(
        list=AsyncMock(
            return_value={"items": [{"id": str(uuid4())}], "total": 1, "page": 1, "page_size": 20}
        )
    )
    scope = AccessScope(actor, company_ids=frozenset({uuid4()}), ownership=OwnershipLevel.OWN)
    ctx = RequestContext(
        SimpleNamespace(), actor, sid, 0, scope, frozenset({"complaints.view"}), None, service
    )
    recheck = AsyncMock(return_value=ctx)
    monkeypatch.setattr(websocket_api, "load_context", recheck)

    @asynccontextmanager
    async def transaction(factory: object, principal: Actor):
        assert principal == actor
        yield SimpleNamespace()

    monkeypatch.setattr(websocket_api, "scoped_transaction", transaction)
    app.state.auth = SimpleNamespace(tokens=tokens, limits=SimpleNamespace(check=AsyncMock()))
    app.state.factory = SimpleNamespace()
    app.state.redis = SimpleNamespace()
    app.state.complaints = service
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set("access_token", access, path="/api")
    return client, ctx, service, recheck


def test_websocket_rejects_cross_origin_and_invalid_filters(fixture: tuple) -> None:
    client, _ctx, service, _recheck = fixture
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(
            "/api/staff/complaints/stream", headers={"origin": "https://evil.test"}
        ),
    ):
        pass
    assert exc.value.code == 1008
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(
            "/api/staff/complaints/stream?company_id=invalid",
            headers={"origin": "https://testserver"},
        ),
    ):
        pass
    assert exc.value.code == 1008
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(
            "/api/staff/complaints/stream", headers={"origin": "http://[broken"}
        ),
    ):
        pass
    assert exc.value.code == 1008
    service.list.assert_not_awaited()


def test_websocket_refresh_uses_same_scope_and_requires_live_permission(fixture: tuple) -> None:
    client, ctx, service, recheck = fixture
    with client.websocket_connect(
        "/api/staff/complaints/stream?search=claim", headers={"origin": "https://testserver"}
    ) as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and len(first["items"]) == 1
        assert service.list.call_args.args[1] == ctx.scope
        assert service.list.call_args.args[2].search == "claim"
        ws.send_text("refresh")
        assert ws.receive_json()["type"] == "snapshot"
        assert recheck.await_count == 2
        ctx.permissions = frozenset()  # Access revoked while socket remains open.
        ws.send_text("refresh")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008
        assert service.list.await_count == 2


def test_websocket_rejects_invalid_message(fixture: tuple) -> None:
    client, _ctx, _service, _recheck = fixture
    with client.websocket_connect(
        "/api/staff/complaints/stream", headers={"origin": "https://testserver"}
    ) as ws:
        ws.receive_json()
        ws.send_text("subscribe:all")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008
