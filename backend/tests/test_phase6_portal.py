"""Phase 6 customer portal: profile, preferences, eligibility and SSE helpers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import ComplaintStatus, InsuranceType
from app.domain.schemas import CustomerProfileUpdate, NotificationPreferences
from app.models.tickets import Complaint
from app.repositories.profile import CustomerProfileRepository
from app.services.complaints import csat_eligible, reopen_eligible
from app.services.realtime import complaint_signature, sse_event


def make_complaint(**overrides: object) -> Complaint:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": uuid4(),
        "created_at": now,
        "ticket_no": "CMP-2026-000777",
        "customer_id": uuid4(),
        "company_id": uuid4(),
        "insurance_type": InsuranceType.HEALTH,
        "policy_number": "POL-123456",
        "subject": "Delayed claim payment",
        "description": "The claim remains unsettled after several weeks.",
        "status": ComplaintStatus.RESOLVED,
        "resolved_at": now,
        "reopened_count": 0,
        "updated_at": now,
    }
    values.update(overrides)
    return Complaint(**values)  # type: ignore[arg-type]


def test_profile_schema_normalizes_email_and_defaults_preferences() -> None:
    item = CustomerProfileUpdate(full_name=" Mona Ali ", email="MONA@Example.COM")
    assert item.email == "mona@example.com"
    assert item.notification_preferences == NotificationPreferences()
    item = CustomerProfileUpdate.model_validate(
        {
            "full_name": "Mona Ali",
            "notification_preferences": {"email": False, "whatsapp": True, "sms": False},
        }
    )
    assert item.notification_preferences.email is False
    with pytest.raises(ValidationError):
        CustomerProfileUpdate(full_name="M", email="not-an-email")
    with pytest.raises(ValidationError):
        CustomerProfileUpdate.model_validate(
            {"full_name": "Mona Ali", "notification_preferences": {"push": True}}
        )


async def test_profile_repository_uses_only_definer_functions() -> None:
    repo = CustomerProfileRepository()
    payload = {"full_name": "Mona Ali", "notification_preferences": {"sms": False}}
    db = SimpleNamespace(scalar=AsyncMock(return_value=json.dumps(payload)))
    assert await repo.profile(db) == payload
    assert "customer_profile()" in str(db.scalar.call_args.args[0])
    db.scalar.reset_mock()
    item = CustomerProfileUpdate(full_name="Mona Ali", whatsapp_available=True)
    await repo.update(db, item)
    sql = str(db.scalar.call_args.args[0])
    assert "update_customer_profile" in sql
    assert db.scalar.call_args.args[1]["whatsapp"] is True
    db.scalar = AsyncMock(return_value=True)
    assert await repo.mark_notification_read(db, uuid4()) is True
    assert "mark_customer_notification_read" in str(db.scalar.call_args.args[0])
    db.scalar = AsyncMock(return_value=None)
    assert await repo.profile(db) is None


def test_reopen_and_csat_windows_mirror_sql_functions() -> None:
    now = datetime.now(UTC)
    assert reopen_eligible(make_complaint(resolved_at=now)) is True
    assert reopen_eligible(make_complaint(resolved_at=now - timedelta(days=15))) is False
    assert reopen_eligible(make_complaint(reopened_count=3, resolved_at=now)) is False
    assert reopen_eligible(make_complaint(status=ComplaintStatus.IN_REVIEW)) is False
    assert csat_eligible(make_complaint(resolved_at=now)) is True
    assert csat_eligible(make_complaint(resolved_at=now - timedelta(days=31))) is False
    assert csat_eligible(make_complaint(resolved_at=None)) is False


def test_sse_signature_changes_with_status_and_messages() -> None:
    detail = {"status": "new", "updated_at": "t1", "assigned_agent_id": None}
    timeline = {"events": [{"id": "e1"}], "messages": []}
    first = complaint_signature(detail, timeline)
    assert first == complaint_signature(detail, timeline)
    assert first != complaint_signature({**detail, "status": "assigned"}, timeline)
    assert first != complaint_signature(
        detail, {"events": [{"id": "e1"}], "messages": [{"id": "m1"}]}
    )
    frame = sse_event("update", {"status": "assigned", "complaint_id": "x"})
    assert frame.startswith("event: update\ndata: ")
    assert json.loads(frame.split("data: ", 1)[1])["status"] == "assigned"


async def test_stream_rejects_staff_claims() -> None:
    from app.services.realtime import stream_customer_complaint

    claims = SimpleNamespace(subject=Actor(uuid4(), "staff"))
    generator = stream_customer_complaint(
        factory=Mock(),  # type: ignore[arg-type]
        redis=Mock(),  # type: ignore[arg-type]
        complaints=Mock(),  # type: ignore[arg-type]
        claims=claims,  # type: ignore[arg-type]
        complaint_id=uuid4(),
        max_polls=1,
    )
    with pytest.raises(Exception) as exc:
        await anext(generator)
    assert getattr(exc.value, "status_code", None) == 403


async def test_stream_emits_snapshot_then_update_on_status_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.realtime as realtime

    statuses = iter(["new", "assigned", "assigned"])

    class FakeComplaints:
        async def detail(self, db: object, scope: object, complaint_id: object) -> dict:
            return {
                "ticket_no": "CMP-2026-000777",
                "status": next(statuses),
                "updated_at": "t",
                "assigned_agent_id": None,
            }

        async def parent(self, db: object, scope: object, complaint_id: object) -> object:
            return SimpleNamespace(id=complaint_id, created_at=datetime.now(UTC))

    class FakeRepo:
        async def timeline(
            self, db: object, scope: object, parent: object, page: int, size: int
        ) -> dict:
            return {"events": [], "messages": [], "page": page}

    @asynccontextmanager
    async def fake_transaction(factory: object, actor: object) -> AsyncIterator[object]:
        yield SimpleNamespace()

    async def fake_context(
        db: object, claims: object, redis: object, complaints: object
    ) -> SimpleNamespace:
        return SimpleNamespace(db=db, scope=SimpleNamespace())

    monkeypatch.setattr(realtime, "scoped_transaction", fake_transaction)
    monkeypatch.setattr(realtime, "load_context", fake_context)
    monkeypatch.setattr(realtime, "ComplaintRepository", FakeRepo)
    claims = SimpleNamespace(subject=Actor(uuid4(), "customer"))
    generator = realtime.stream_customer_complaint(
        factory=Mock(),  # type: ignore[arg-type]
        redis=Mock(),  # type: ignore[arg-type]
        complaints=FakeComplaints(),  # type: ignore[arg-type]
        claims=claims,  # type: ignore[arg-type]
        complaint_id=uuid4(),
        poll_seconds=0,
        max_polls=3,
    )
    frames = [frame async for frame in generator]
    assert frames[0].startswith("event: snapshot")
    assert '"status": "new"' in frames[0]
    assert frames[1].startswith("event: update")
    assert '"status": "assigned"' in frames[1]
    assert frames[2].startswith(": heartbeat")


async def test_customer_scope_hides_other_customers_tickets() -> None:
    scope = AccessScope.for_customer(Actor(uuid4(), "customer"))
    sql = str(scope.predicate().compile(compile_kwargs={"literal_binds": True}))
    assert "complaints.customer_id" in sql
