"""Direct endpoint tests for Phase 4 admin, staff and public boundaries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response

from app.api import admin_integrations, integration_webhooks, integrations, public_api
from app.domain.schemas import (
    AIAnalysisRequest,
    AIReviewRequest,
    ApiKeyCreate,
    IntegrationConfigSpec,
    ReplyRequest,
    SendComplaintEmailRequest,
    VoiceFollowupRequest,
    WebhookCreate,
    WhatsAppSendRequest,
)
from app.security.blobs import RECORDING, BlobCipher


def make_request(service: Any, *, blobs: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                integrations=service,
                blobs=blobs,
                auth=SimpleNamespace(limits=None),
                factory=object(),
            )
        ),
        client=SimpleNamespace(host="192.0.2.10"),
    )


def make_context(permissions: set[str] | None = None, db: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        db=db or SimpleNamespace(),
        scope=SimpleNamespace(actor=SimpleNamespace(id=uuid4(), scope="staff")),
        actor=SimpleNamespace(id=uuid4(), scope="staff"),
        permissions=frozenset(permissions or set()),
    )


async def test_admin_endpoints_delegate_and_hide_one_time_secrets() -> None:
    service = SimpleNamespace(
        list_configs=AsyncMock(return_value=[{"provider": "smtp", "enabled": True}]),
        save_config=AsyncMock(return_value={"enabled": True}),
        list_webhooks=AsyncMock(return_value=[]),
        create_webhook=AsyncMock(return_value={"signing_secret": "one-time-secret"}),
        deactivate_webhook=AsyncMock(return_value=None),
        list_api_keys=AsyncMock(return_value=[]),
        create_api_key=AsyncMock(return_value={"token": "ipk_secret"}),
        revoke_api_key=AsyncMock(return_value=True),
        dead_letters=AsyncMock(return_value=[]),
        requeue_dead_letter=AsyncMock(return_value=None),
    )
    request = make_request(service)
    ctx = make_context()
    assert await admin_integrations.integrations(request, ctx) == [
        {"provider": "smtp", "enabled": True}
    ]
    config = IntegrationConfigSpec(provider="mock_llm", enabled=True, credentials={})
    await admin_integrations.configure_integration(config, request, ctx)
    service.save_config.assert_awaited_once_with(
        ctx.db,
        company_id=None,
        provider="mock_llm",
        credentials={},
        enabled=True,
        clear_credentials=False,
    )
    assert await admin_integrations.webhooks(request, ctx) == []
    hook = WebhookCreate(
        url="https://hooks.example.test/events", event_types={"complaint.status_changed"}
    )
    response = Response()
    created = await admin_integrations.create_webhook(hook, request, response, ctx)
    assert created["signing_secret"] == "one-time-secret"
    assert response.headers["cache-control"] == "no-store"
    await admin_integrations.deactivate_webhook(uuid4(), request, ctx)
    assert await admin_integrations.api_keys(request, ctx) == []
    key = ApiKeyCreate(permissions={"complaints.view"}, all_companies=True)
    key_response = Response()
    assert await admin_integrations.create_api_key(key, request, key_response, ctx) == {
        "token": "ipk_secret"
    }
    assert key_response.headers["cache-control"] == "no-store"
    await admin_integrations.revoke_api_key(uuid4(), request, ctx)
    assert await admin_integrations.integration_dead_letters(request, 10, ctx) == []
    assert await admin_integrations.retry_dead_letter(uuid4(), request, ctx) == {"queued": True}
    service.revoke_api_key.return_value = False
    with pytest.raises(HTTPException) as missing_key:
        await admin_integrations.revoke_api_key(uuid4(), request, ctx)
    assert missing_key.value.status_code == 404


async def test_staff_channel_endpoints_enforce_ai_and_channel_permissions() -> None:
    service = SimpleNamespace(
        send_complaint_by_email=AsyncMock(return_value={"state": "queued"}),
        send_customer_email=AsyncMock(return_value={"state": "queued"}),
        send_whatsapp=AsyncMock(return_value={"state": "queued"}),
        request_ai_analysis=AsyncMock(return_value={"state": "queued"}),
        list_ai_insights=AsyncMock(return_value=[]),
        similar_tickets=AsyncMock(return_value=[]),
        review_ai_insight=AsyncMock(return_value={"review_status": "accepted"}),
        send_approved_reply=AsyncMock(return_value={"state": "queued"}),
        unified_inbox=AsyncMock(return_value={"items": []}),
        schedule_voice_followup=AsyncMock(return_value={"call_log_id": uuid4()}),
        status=AsyncMock(return_value={"state": "sent"}),
    )
    request = make_request(service)
    complaint_id = uuid4()
    ctx = make_context(
        {
            "complaints.view",
            "integrations.ai_call",
            "integrations.send_email",
            "integrations.send_whatsapp",
        }
    )
    assert await integrations.send_complaint_email(
        complaint_id, SendComplaintEmailRequest(), request, "email-key-123", ctx
    ) == {"state": "queued"}
    assert await integrations.send_customer_email_reply(
        complaint_id, ReplyRequest(body="Thanks"), request, "en", "reply-key-123", ctx
    ) == {"state": "queued"}
    assert await integrations.send_whatsapp_message(
        complaint_id,
        WhatsAppSendRequest(body="Update", template_name="ticket_update"),
        request,
        "whatsapp-key-123",
        ctx,
    ) == {"state": "queued"}
    assert await integrations.request_ai_analysis(
        complaint_id, AIAnalysisRequest(locale="ar"), request, "analysis-key-123", ctx
    ) == {"state": "queued"}
    assert await integrations.ai_insights(complaint_id, request, ctx) == []
    assert await integrations.similar_complaints(complaint_id, request, 5, ctx) == []
    assert await integrations.review_ai_suggestion(
        complaint_id, uuid4(), AIReviewRequest(accept=True), request, ctx
    ) == {"review_status": "accepted"}
    assert await integrations.send_approved_ai_reply(
        complaint_id, uuid4(), request, "email", "approved-key-123", ctx
    ) == {"state": "queued"}
    assert await integrations.unified_inbox(complaint_id, request, ctx) == {"items": []}
    assert service.unified_inbox.await_args.kwargs["include_calls"] is True
    assert await integrations.schedule_voice_followup(
        complaint_id,
        VoiceFollowupRequest(scheduled_at=datetime.now(UTC)),
        request,
        "voice-key-123",
        ctx,
    )
    assert await integrations.integration_job_status(uuid4(), request, ctx) == {"state": "sent"}

    ctx.permissions = frozenset({"integrations.ai_call"})
    with pytest.raises(HTTPException) as denied:
        await integrations.send_approved_ai_reply(
            complaint_id, uuid4(), request, "whatsapp", "approved-key-456", ctx
        )
    assert denied.value.status_code == 403
    assert service.send_approved_reply.await_count == 1


async def test_recording_route_is_no_store_audited_and_ticket_scoped() -> None:
    call_id = uuid4()
    complaint_id = uuid4()
    blobs = BlobCipher(b"r" * 32)
    call = SimpleNamespace(
        id=call_id,
        complaint_id=complaint_id,
        complaint_created_at=datetime.now(UTC),
        recording_content=blobs.encrypt(b"audio-bytes", purpose=RECORDING, resource_id=call_id),
    )
    service = SimpleNamespace()
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=call),
        add=Mock(),
        flush=AsyncMock(return_value=None),
    )
    ctx = make_context({"integrations.ai_call"}, db)
    request = make_request(service, blobs=blobs)
    response = await integrations.call_recording(complaint_id, call_id, request, ctx)
    assert response.body == b"audio-bytes"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert db.add.call_args.args[0].action == "call_recording_accessed"
    # A row whose ciphertext belongs to another call is never served.
    call.recording_content = blobs.encrypt(b"audio-bytes", purpose=RECORDING, resource_id=uuid4())
    with pytest.raises(HTTPException) as unreadable:
        await integrations.call_recording(complaint_id, call_id, request, ctx)
    assert unreadable.value.status_code == 503
    db.scalar.return_value = None
    with pytest.raises(HTTPException) as not_found:
        await integrations.call_recording(complaint_id, call_id, request, ctx)
    assert not_found.value.status_code == 404


async def test_webhook_routes_bound_payloads_and_use_short_purpose_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(
        receive_webhook=AsyncMock(return_value={"accepted": 1}),
        verify_whatsapp_subscription=AsyncMock(return_value="challenge"),
    )
    request = make_request(service)
    request.headers = {"content-length": "2", "x-webhook-signature": "signed"}
    request._body = b"{}"

    async def body() -> bytes:
        return request._body

    request.body = body
    transactions: list[str] = []

    @asynccontextmanager
    async def transaction(_factory: Any, purpose: str):
        transactions.append(purpose)
        yield object()

    monkeypatch.setattr(integration_webhooks, "purpose_transaction", transaction)
    result = await integration_webhooks.provider_webhook("email_webhook", uuid4(), request)
    assert result == {"accepted": 1}
    assert transactions == ["webhook"]
    request.headers["content-length"] = str(integration_webhooks._MAX_BODY_BYTES + 1)
    with pytest.raises(HTTPException) as too_large:
        await integration_webhooks.provider_webhook("email_webhook", uuid4(), request)
    assert too_large.value.status_code == 413
    request.headers = {}
    response = await integration_webhooks.verify_whatsapp_webhook(
        uuid4(), request, "subscribe", "token", "challenge"
    )
    assert response.body == b"challenge"
    assert transactions[-1] == "webhook"


async def test_public_api_key_route_checks_hash_permissions_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_identity = {"permission_codes": ["complaints.view"]}
    ticket = {
        "id": uuid4(),
        "ticket_no": "CMP-2026-000001",
        "company_id": uuid4(),
        "insurance_type": "health",
        "status": "new",
        "priority": "normal",
        "subject": "Claim question",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    captured: dict[str, Any] = {}

    class Repository:
        async def api_key_identity(self, _db: Any, prefix: str, digest: bytes) -> Any:
            captured["prefix"] = prefix
            captured["digest"] = digest
            return expected_identity

        async def api_key_ticket(self, _db: Any, prefix: str, digest: bytes, ticket_no: str) -> Any:
            assert prefix == captured["prefix"]
            assert digest == captured["digest"]
            assert ticket_no == ticket["ticket_no"]
            return ticket

    @asynccontextmanager
    async def transaction(_factory: Any, purpose: str):
        assert purpose == "guest"
        yield object()

    monkeypatch.setattr(public_api, "IntegrationRepository", Repository)
    monkeypatch.setattr(public_api, "purpose_transaction", transaction)
    request = make_request(SimpleNamespace())
    response = Response()
    token = "ipk_abcdefghijklmnop." + "s" * 43
    result = await public_api.api_key_complaint(
        request, response, ticket["ticket_no"], f"ApiKey {token}"
    )
    assert result["ticket_no"] == ticket["ticket_no"]
    assert response.headers["cache-control"] == "no-store"
    with pytest.raises(HTTPException) as missing:
        await public_api.api_key_complaint(request, Response(), ticket["ticket_no"], None)
    assert missing.value.status_code == 401
    expected_identity["permission_codes"] = []
    with pytest.raises(HTTPException) as denied:
        await public_api.api_key_complaint(
            request, Response(), ticket["ticket_no"], f"ApiKey {token}"
        )
    assert denied.value.status_code == 403
