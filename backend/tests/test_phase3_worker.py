"""Worker automation uses only worker-owned database entry points and fail-closed email."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app import worker
from app.services.providers import SMTPDelivery


async def test_automation_jobs_call_restricted_definer_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def query(engine: object, sql: str, params: dict):
        calls.append((sql, params))
        return [SimpleNamespace(processed=3)]

    monkeypatch.setattr(worker, "_worker_query", query)
    context = {"engine": object()}
    assert await worker.assign_tickets(context) == "assigned 3 ticket(s)"
    assert await worker.check_sla(context) == "breached 3 SLA deadline(s)"
    assert "auto_assign_tickets" in calls[0][0]
    assert "process_sla_breaches" in calls[1][0]
    assert calls[0][1] == calls[1][1] == {"limit": 20}


async def test_insurer_delivery_retry_and_absent_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = [
        SimpleNamespace(
            notice_id=uuid4(),
            recipient_email="insurer@test.invalid",
            ticket_no="CMP-2026-000001",
            reason="Human-reviewed case",
        )
    ]
    outputs: list[dict] = []

    async def query(engine: object, sql: str, params: dict):
        if "claim_insurer_notices" in sql:
            return claims
        outputs.append(params)
        return [(True,)]

    monkeypatch.setattr(worker, "_worker_query", query)
    sender = SimpleNamespace(send_insurer=AsyncMock(return_value=False))
    context = {"engine": object(), "delivery": sender}
    assert await worker.deliver_insurer(context) == "processed 1 insurer notice(s)"
    assert outputs[-1] == {"id": claims[0].notice_id, "state": "pending_provider"}
    sender.send_insurer.return_value = True
    await worker.deliver_insurer(context)
    assert outputs[-1]["state"] == "sent"
    sender.send_insurer.side_effect = RuntimeError("SMTP down")
    await worker.deliver_insurer(context)
    assert outputs[-1]["state"] == "retry"


async def test_smtp_adapter_never_claims_unsent_insurer_email() -> None:
    delivery = SMTPDelivery(
        SimpleNamespace(
            smtp_host=None,
            smtp_port=25,
            smtp_from="sender@test.invalid",
            smtp_username=None,
            smtp_password=None,
            smtp_start_tls=False,
        )
    )
    assert not await delivery.send_insurer("insurer@test.invalid", "CMP-2026-000001", "Reason")
