"""Staff live ticket stream: signature changes and per-poll revalidation."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.services.realtime as realtime
from app.db import Actor
from app.services.realtime import complaint_signature, stream_staff_complaint


def test_signature_changes_with_new_message_and_attachment_scan_state() -> None:
    detail = {"status": "new", "updated_at": "t1", "assigned_agent_id": None}
    timeline = {"events": [], "messages": [{"id": "m1"}]}
    base = complaint_signature(detail, timeline)
    assert base != complaint_signature(
        detail, {"events": [], "messages": [{"id": "m1"}, {"id": "m2"}]}
    )
    pending = SimpleNamespace(id=uuid4(), scan_status="pending")
    clean = SimpleNamespace(id=pending.id, scan_status="clean")
    assert complaint_signature(detail, timeline, [pending]) != complaint_signature(
        detail, timeline, [clean]
    )


async def test_staff_stream_rejects_customer_claims() -> None:
    claims = SimpleNamespace(subject=Actor(uuid4(), "customer"))
    generator = stream_staff_complaint(
        factory=Mock(),  # type: ignore[arg-type]
        redis=Mock(),  # type: ignore[arg-type]
        complaints=Mock(),  # type: ignore[arg-type]
        claims=claims,  # type: ignore[arg-type]
        complaint_id=uuid4(),
        max_polls=1,
    )
    with pytest.raises(HTTPException) as exc:
        await anext(generator)
    assert exc.value.status_code == 403


async def test_staff_stream_emits_update_when_customer_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complaint_id = uuid4()
    states = [
        (
            {"status": "new", "ticket_no": "CMP-1", "updated_at": "t1", "assigned_agent_id": None},
            {"events": [], "messages": [{"id": "m1"}]},
        ),
        (
            {"status": "new", "ticket_no": "CMP-1", "updated_at": "t2", "assigned_agent_id": None},
            {"events": [], "messages": [{"id": "m1"}, {"id": "m2"}]},
        ),
    ]
    calls = {"n": 0}

    @asynccontextmanager
    async def fake_transaction(factory: object, actor: object):
        yield SimpleNamespace()

    async def fake_load_context(db: object, claims: object, redis: object, complaints: object):
        return SimpleNamespace(
            permissions=frozenset({"complaints.view"}),
            scope=SimpleNamespace(),
        )

    complaints = SimpleNamespace()
    complaints.detail = AsyncMock(side_effect=lambda *a: states[min(calls["n"], 1)][0])
    complaints.parent = AsyncMock(return_value=SimpleNamespace(id=complaint_id))

    timeline_calls = {"n": 0}

    async def fake_timeline(
        self: object, db: object, scope: object, parent: object, page: int, size: int
    ):
        index = min(timeline_calls["n"], 1)
        timeline_calls["n"] += 1
        calls["n"] = timeline_calls["n"] - 1
        return states[index][1]

    async def fake_attachments(self: object, db: object, scope: object, parent: object):
        return []

    monkeypatch.setattr(realtime, "scoped_transaction", fake_transaction)
    monkeypatch.setattr(realtime, "load_context", fake_load_context)
    monkeypatch.setattr(realtime.ComplaintRepository, "timeline", fake_timeline)
    monkeypatch.setattr(realtime.ComplaintRepository, "attachments", fake_attachments)

    claims = SimpleNamespace(subject=Actor(uuid4(), "staff"))
    generator = stream_staff_complaint(
        factory=Mock(),  # type: ignore[arg-type]
        redis=Mock(),  # type: ignore[arg-type]
        complaints=complaints,  # type: ignore[arg-type]
        claims=claims,  # type: ignore[arg-type]
        complaint_id=complaint_id,
        poll_seconds=0,
        max_polls=2,
    )
    frames = [frame async for frame in generator]
    assert frames[0].startswith("event: snapshot")
    assert frames[1].startswith("event: update")
    payload = json.loads(frames[1].split("data: ", 1)[1])
    assert payload["complaint_id"] == str(complaint_id)
    assert "body" not in payload  # Signatures only; message bodies never leak.


async def test_staff_stream_rechecks_permission_each_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def fake_transaction(factory: object, actor: object):
        yield SimpleNamespace()

    async def fake_load_context(db: object, claims: object, redis: object, complaints: object):
        return SimpleNamespace(permissions=frozenset(), scope=SimpleNamespace())

    monkeypatch.setattr(realtime, "scoped_transaction", fake_transaction)
    monkeypatch.setattr(realtime, "load_context", fake_load_context)
    claims = SimpleNamespace(subject=Actor(uuid4(), "staff"))
    generator = stream_staff_complaint(
        factory=Mock(),  # type: ignore[arg-type]
        redis=Mock(),  # type: ignore[arg-type]
        complaints=SimpleNamespace(),  # type: ignore[arg-type]
        claims=claims,  # type: ignore[arg-type]
        complaint_id=uuid4(),
        poll_seconds=0,
        max_polls=1,
    )
    with pytest.raises(HTTPException) as exc:
        await anext(generator)
    assert exc.value.status_code == 403


def test_staff_stream_route_registered() -> None:
    from app.api import staff

    paths = {route.path for route in staff.router.routes}
    assert "/api/staff/complaints/{complaint_id}/stream" in paths
