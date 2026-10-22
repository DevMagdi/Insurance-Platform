"""Focused Phase 4 service/repository tests with no live database or providers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import (
    CommunicationChannel,
    MessageDirection,
    MessageVisibility,
    OwnershipLevel,
)
from app.domain.schemas import (
    AIAnalysisRequest,
    SendComplaintEmailRequest,
    VoiceFollowupRequest,
    WhatsAppSendRequest,
)
from app.repositories.integrations import IntegrationRepository
from app.security.integration_crypto import SecretCipher
from app.services import integrations


@pytest.fixture
def cipher() -> SecretCipher:
    return SecretCipher(b"p" * 32)


class Rows:
    def __init__(self, values: list[Any] | None = None) -> None:
        self.values = values or []

    def one(self) -> Any:
        return self.values[0]

    def one_or_none(self) -> Any | None:
        return self.values[0] if self.values else None

    def all(self) -> list[Any]:
        return self.values

    def __iter__(self):
        return iter(self.values)


def row(values: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(_mapping=values, **values)


def make_scope(scope: str = "staff") -> AccessScope:
    actor = Actor(uuid4(), scope)  # type: ignore[arg-type]
    if scope == "customer":
        return AccessScope.for_customer(actor)
    return AccessScope(
        actor=actor,
        all_companies=True,
        ownership=OwnershipLevel.ALL,
    )


def make_complaint() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        company_id=uuid4(),
        customer_id=uuid4(),
        created_at=datetime.now(UTC),
        ticket_no="CMP-2026-000123",
        subject="Claim follow-up",
        description="Customer 29876543210987 asks for a status update.",
        policy_number="POL-998877",
        insurance_type=SimpleNamespace(value="health"),
        status=SimpleNamespace(value="in_review"),
    )


def make_repository(**overrides: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "enqueue_job": AsyncMock(
            return_value={"job_id": uuid4(), "state": "queued", "created": True}
        ),
        "company_contacts": AsyncMock(return_value=["claims@insurer.example.test"]),
        "webhooks_for_event": AsyncMock(return_value=[]),
        "enqueue_webhook": AsyncMock(return_value=uuid4()),
        "save_config": AsyncMock(return_value=uuid4()),
        "list_configs": AsyncMock(return_value=[]),
        "integration_credentials": AsyncMock(return_value=None),
        "record_connection_test": AsyncMock(return_value=True),
        "integration_job_counts": AsyncMock(return_value=0),
        "save_webhook": AsyncMock(return_value=uuid4()),
        "list_webhooks": AsyncMock(return_value=[]),
        "webhook_config": AsyncMock(return_value=None),
        "create_api_key": AsyncMock(return_value=uuid4()),
        "list_api_keys": AsyncMock(return_value=[]),
        "revoke_api_key": AsyncMock(return_value=True),
        "job_status": AsyncMock(return_value={"id": uuid4(), "state": "sent"}),
        "dead_letters": AsyncMock(return_value=[]),
        "requeue_dead_letter": AsyncMock(return_value=True),
        "queue_call_log": AsyncMock(return_value=uuid4()),
        "set_voice_consent": AsyncMock(return_value=datetime.now(UTC)),
        "review_ai_insight": AsyncMock(return_value=True),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class ComplaintRepository:
    def __init__(self, complaint: Any) -> None:
        self.complaint = complaint

    async def get(self, *_args: Any) -> Any:
        return self.complaint


class AttachmentRepository:
    def __init__(self, attachment: Any) -> None:
        self.attachment = attachment

    async def clean_attachment(self, *_args: Any) -> Any:
        return self.attachment


async def test_integration_repository_wraps_scoped_database_functions() -> None:
    now = datetime.now(UTC)
    key_id = uuid4()
    hook_id = uuid4()
    call_id = uuid4()
    mapping = {"id": uuid4(), "state": "queued"}
    execute_results = [
        Rows([row({"id": uuid4(), "provider": "smtp"})]),  # configs
        Rows([row({"config_company_id": None, "config_ciphertext": b"cipher"})]),
        Rows([row(mapping)]),  # inbound
        Rows([row(mapping)]),  # outbound
        Rows([row(mapping)]),  # job status
        Rows([row({"id": hook_id, "url": "https://hook.example.test"})]),
        Rows([row(mapping)]),  # API key listing
        Rows([row({"owner_staff_id": uuid4(), "permission_codes": ["complaints.view"]})]),
        Rows([row({"id": uuid4(), "ticket_no": "CMP-2026-000123"})]),
        Rows([row(mapping)]),  # DLQ
        Rows([row(mapping)]),  # worker inbound
    ]
    scalar_results: list[Any] = [
        key_id,  # save config
        ["claims@example.test"],  # contact emails
        call_id,  # create call
        now,  # consent
        True,  # review
        hook_id,  # webhook save
        uuid4(),  # enqueue webhook
        uuid4(),  # API key create
        True,  # API key revoke
        True,  # dead letter replay
    ]

    class ScalarDB:
        def __init__(self) -> None:
            self.execute = AsyncMock(side_effect=execute_results)
            self.scalar = AsyncMock(side_effect=scalar_results)
            self.scalars = AsyncMock(return_value=Rows([hook_id]))

    db = ScalarDB()
    repository = IntegrationRepository()
    company_id = uuid4()
    complaint_id = uuid4()
    staff_id = uuid4()

    assert await repository.save_config(db, company_id, "smtp", b"cipher", True) == key_id
    assert await repository.list_configs(db) == [
        {"id": execute_results[0].one().id, "provider": "smtp"}
    ]
    assert await repository.webhook_config(db, company_id, "smtp") == (None, b"cipher")
    assert (
        await repository.enqueue_inbound(
            db, company_id, "email_webhook", "inbound_email", b"cipher", b"i" * 32, b"p" * 32
        )
        == mapping
    )
    assert (
        await repository.enqueue_job(
            db,
            job_kind="send_email",
            company_id=company_id,
            complaint_id=complaint_id,
            complaint_created_at=now,
            sender_staff_id=staff_id,
            message_id=None,
            call_log_id=None,
            payload_ciphertext=b"cipher",
            idempotency_digest=b"i" * 32,
            payload_digest=b"p" * 32,
        )
        == mapping
    )
    assert await repository.job_status(db, uuid4()) == mapping
    assert await repository.company_contacts(db, company_id) == ["claims@example.test"]
    assert (
        await repository.queue_call_log(
            db,
            call_id=call_id,
            complaint_id=complaint_id,
            complaint_created_at=now,
            sender_staff_id=staff_id,
            scheduled_at=now,
        )
        == call_id
    )
    assert await repository.set_voice_consent(db, complaint_id, now, True) == now
    assert await repository.review_ai_insight(db, complaint_id, now, uuid4(), True)
    assert (
        await repository.save_webhook(
            db,
            webhook_id=hook_id,
            company_id=company_id,
            url="https://hook.example.test",
            events=["complaint.status_changed"],
            ciphertext=b"cipher",
            active=True,
        )
        == hook_id
    )
    assert await repository.list_webhooks(db)
    assert await repository.webhooks_for_event(
        db, complaint_id, now, "complaint.status_changed"
    ) == [hook_id]
    assert await repository.enqueue_webhook(
        db,
        webhook_id=hook_id,
        complaint_id=complaint_id,
        created_at=now,
        company_id=company_id,
        event="complaint.status_changed",
        ciphertext=b"cipher",
        idempotency_digest=b"i" * 32,
        payload_digest=b"p" * 32,
    )
    assert await repository.create_api_key(
        db,
        prefix="ipk_abcdefghijklmnop",
        key_hash=b"h" * 32,
        permissions=["complaints.view"],
        all_companies=True,
        company_ids=[],
        expires_at=None,
    )
    assert await repository.list_api_keys(db)
    assert await repository.revoke_api_key(db, uuid4())
    assert await repository.api_key_identity(db, "ipk_abcdefghijklmnop", b"h" * 32)
    assert await repository.api_key_ticket(db, "ipk_abcdefghijklmnop", b"h" * 32, "CMP-2026-000123")
    assert await repository.dead_letters(db)
    assert await repository.requeue_dead_letter(db, uuid4())
    assert (
        await repository.enqueue_worker_inbound(
            db,
            company_id=company_id,
            job_kind="inbound_email",
            ciphertext=b"cipher",
            idempotency_digest=b"i" * 32,
            payload_digest=b"p" * 32,
        )
        == mapping
    )


async def test_repository_rejects_untyped_uuid_results() -> None:
    class DB:
        def __init__(self) -> None:
            self.scalar = AsyncMock(return_value=None)

    repository = IntegrationRepository()
    with pytest.raises(PermissionError):
        await repository.save_config(DB(), None, "mock_llm", b"cipher", True)
    with pytest.raises(PermissionError):
        await repository.queue_call_log(
            DB(),
            call_id=uuid4(),
            complaint_id=uuid4(),
            complaint_created_at=datetime.now(UTC),
            sender_staff_id=uuid4(),
            scheduled_at=datetime.now(UTC),
        )


async def test_email_and_whatsapp_services_queue_scoped_messages_and_hmac_webhooks(
    cipher: SecretCipher,
) -> None:
    complaint = make_complaint()
    scope = make_scope()
    actor_id = scope.actor.id
    attachment_id = uuid4()
    attachment = SimpleNamespace(
        id=attachment_id,
        complaint_id=complaint.id,
        complaint_created_at=complaint.created_at,
    )
    repository = make_repository(
        webhooks_for_event=AsyncMock(return_value=[uuid4()]),
        enqueue_webhook=AsyncMock(return_value=uuid4()),
    )
    service = integrations.IntegrationService(
        cipher,
        b"s" * 32,
        repository=repository,
        complaints=ComplaintRepository(complaint),
        attachments=AttachmentRepository(attachment),
    )
    event_id = uuid4()
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Rows()),
        scalar=AsyncMock(side_effect=[event_id, "customer@example.test", event_id, 1, event_id]),
    )
    result = await service.send_complaint_by_email(
        db,
        scope,
        complaint.id,
        SendComplaintEmailRequest(
            locale="ar", agent_note="Follow-up", attachment_ids=[attachment_id]
        ),
        "email-idempotency-01",
    )
    assert result["state"] == "queued"
    email_job = repository.enqueue_job.await_args_list[0].kwargs
    assert email_job["sender_staff_id"] == actor_id
    digest = service._digest("send_email", scope, complaint.id, "email-idempotency-01")
    email_payload = cipher.decrypt_json(
        email_job["payload_ciphertext"], associated_data=integrations.job_aad(digest)
    )
    assert email_payload["recipients"] == ["claims@insurer.example.test"]
    assert email_payload["locale"] == "ar"
    assert email_payload["attachment_ids"] == [str(attachment_id)]
    assert "29876543210987" not in email_payload["body"]
    assert repository.enqueue_webhook.await_count == 1

    email_reply = await service.send_customer_email(
        db, scope, complaint.id, "We are checking the case.", "en", "reply-idempotency-01"
    )
    assert email_reply["message_id"]
    reply_job = repository.enqueue_job.await_args_list[1].kwargs
    reply_digest = service._digest("send_email", scope, complaint.id, "reply-idempotency-01")
    reply_payload = cipher.decrypt_json(
        reply_job["payload_ciphertext"], associated_data=integrations.job_aad(reply_digest)
    )
    assert reply_payload["subject"].startswith("Response to request")

    customer = SimpleNamespace(mobile="+201012345678", whatsapp_available=True)
    db.execute.return_value = Rows([customer])
    whatsapp = await service.send_whatsapp(
        db,
        scope,
        complaint.id,
        WhatsAppSendRequest(body="Status update", template_name="ticket_update"),
        "whatsapp-idempotency-01",
    )
    assert whatsapp["state"] == "queued"
    whatsapp_job = repository.enqueue_job.await_args_list[2].kwargs
    assert whatsapp_job["sender_staff_id"] == actor_id
    assert whatsapp_job["job_kind"] == "send_whatsapp"
    assert repository.enqueue_webhook.await_count == 3


async def test_whatsapp_free_text_window_and_customer_opt_in_are_enforced(
    cipher: SecretCipher,
) -> None:
    complaint = make_complaint()
    scope = make_scope()
    repository = make_repository()
    service = integrations.IntegrationService(
        cipher, b"s" * 32, repository=repository, complaints=ComplaintRepository(complaint)
    )
    customer = SimpleNamespace(mobile="+201012345678", whatsapp_available=True)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Rows([customer])),
        scalar=AsyncMock(return_value=0),
    )
    with pytest.raises(HTTPException) as outside_window:
        await service.send_whatsapp(
            db,
            scope,
            complaint.id,
            WhatsAppSendRequest(body="Free text"),
            "whatsapp-idempotency-02",
        )
    assert outside_window.value.status_code == 409
    assert repository.enqueue_job.await_count == 0
    customer.whatsapp_available = False
    with pytest.raises(HTTPException) as opted_out:
        await service.send_whatsapp(
            db,
            scope,
            complaint.id,
            WhatsAppSendRequest(body="Template", template_name="ticket_update"),
            "whatsapp-idempotency-03",
        )
    assert opted_out.value.status_code == 409


async def test_ai_analysis_insights_approval_and_channel_send_requirements(
    cipher: SecretCipher,
) -> None:
    complaint = make_complaint()
    scope = make_scope()
    repository = make_repository()
    service = integrations.IntegrationService(
        cipher, b"s" * 32, repository=repository, complaints=ComplaintRepository(complaint)
    )
    job_id = uuid4()
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=["Customer Example", None]),
        execute=AsyncMock(return_value=Rows()),
        scalars=AsyncMock(return_value=[]),
    )
    analysis = await service.request_ai_analysis(
        db, scope, complaint.id, AIAnalysisRequest(locale="en"), "ai-idempotency-01"
    )
    assert analysis["state"] == "queued"
    ai_job = repository.enqueue_job.await_args.kwargs
    ai_digest = service._digest("ai_analyze", scope, complaint.id, "ai-idempotency-01")
    ai_payload = cipher.decrypt_json(
        ai_job["payload_ciphertext"], associated_data=integrations.job_aad(ai_digest)
    )
    assert "[NATIONAL_ID]" in ai_payload["text"]
    assert "Customer Example" not in ai_payload["text"]

    insight_payload = {"reply": "We will follow up.", "locale": "en"}
    insight_id = uuid4()
    insight = SimpleNamespace(
        id=insight_id,
        kind="reply",
        integration_job_id=job_id,
        payload_ciphertext=cipher.encrypt_json(
            insight_payload, associated_data=integrations.ai_insight_aad(job_id, "reply")
        ),
        confidence=0.9,
        provider_model="mock-llm-v1",
        review_status="accepted",
        created_at=datetime.now(UTC),
    )
    db.scalars.return_value = [insight]
    insights = await service.list_ai_insights(db, scope, complaint.id)
    assert insights[0]["payload"] == insight_payload
    assert await service.review_ai_insight(db, scope, complaint.id, insight_id, True)
    db.scalar.side_effect = [insight, "customer@example.test", None]
    sent = await service.send_approved_reply(
        db, scope, complaint.id, insight_id, "email", "approved-reply-01"
    )
    assert sent["state"] == "queued"
    db.scalar.side_effect = None
    db.scalar.return_value = None
    with pytest.raises(HTTPException) as forbidden:
        await service.send_approved_reply(
            db, scope, complaint.id, uuid4(), "email", "approved-reply-02"
        )
    assert forbidden.value.status_code == 409


async def test_similar_ticket_search_voice_consent_scheduling_and_unified_inbox(
    cipher: SecretCipher,
) -> None:
    complaint = make_complaint()
    staff_scope = make_scope()
    repository = make_repository()
    service = integrations.IntegrationService(
        cipher, b"s" * 32, repository=repository, complaints=ComplaintRepository(complaint)
    )
    match = {"ticket_no": "CMP-2026-000124", "similarity": 0.8}
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Rows([SimpleNamespace(_mapping=match)]),
                Rows(
                    [
                        SimpleNamespace(
                            mobile="+201012345678",
                            voice_followup_opt_in=True,
                            voice_consent_at=datetime.now(UTC),
                        )
                    ]
                ),
            ]
        ),
        scalars=AsyncMock(return_value=[]),
        scalar=AsyncMock(return_value=None),
    )
    assert await service.similar_tickets(db, staff_scope, complaint.id) == [match]
    scheduled = await service.schedule_voice_followup(
        db,
        staff_scope,
        complaint.id,
        VoiceFollowupRequest(scheduled_at=datetime.now(UTC) + timedelta(hours=1), language="en"),
        "voice-idempotency-01",
    )
    assert scheduled["call_log_id"]
    assert repository.queue_call_log.await_args.kwargs["sender_staff_id"] == staff_scope.actor.id
    call_payload = cipher.decrypt_json(
        repository.enqueue_job.await_args.kwargs["payload_ciphertext"],
        associated_data=integrations.job_aad(
            repository.enqueue_job.await_args.kwargs["idempotency_digest"]
        ),
    )
    assert call_payload["consent_confirmed"] is True
    assert "human agent" in call_payload["script"]

    customer_scope = AccessScope.for_customer(Actor(complaint.customer_id, "customer"))
    repository.set_voice_consent.return_value = datetime.now(UTC)
    consent = await service.record_voice_consent(db, customer_scope, complaint.id, True)
    assert consent["enabled"] is True
    with pytest.raises(HTTPException) as staff_consent:
        await service.record_voice_consent(db, staff_scope, complaint.id, True)
    assert staff_consent.value.status_code == 403

    message = SimpleNamespace(
        id=uuid4(),
        channel=CommunicationChannel.EMAIL,
        direction=MessageDirection.OUTBOUND,
        body="Status update",
        visibility=MessageVisibility.PUBLIC,
        sender_staff_id=staff_scope.actor.id,
        sender_customer_id=None,
        sender_external=None,
        delivery_status="delivered",
        delivered_at=datetime.now(UTC),
        read_at=None,
        created_at=datetime.now(UTC),
    )
    call_id = uuid4()
    call = SimpleNamespace(
        id=call_id,
        direction=MessageDirection.OUTBOUND,
        status="completed",
        outcome="completed",
        transcript_ciphertext=cipher.encrypt(
            b"We called about the ticket.",
            associated_data=integrations.call_transcript_aad(call_id),
        ),
        consent_at=datetime.now(UTC),
        scheduled_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        sender_staff_id=staff_scope.actor.id,
    )
    db.scalars.side_effect = [Rows([message]), Rows([])]
    # Calls now come back as (row, has_recording): the recording column is
    # deferred, so whether audio exists is answered in SQL rather than by
    # loading it.
    db.execute.side_effect = [Rows([(call, False)])]
    inbox = await service.unified_inbox(db, staff_scope, complaint.id, include_calls=True)
    assert len(inbox["items"]) == 2
    assert any(item.get("transcript") == "We called about the ticket." for item in inbox["items"])
    assert any(item.get("has_recording") is False for item in inbox["items"])


async def test_integration_admin_config_webhook_key_and_dlq_lifecycle(
    cipher: SecretCipher,
) -> None:
    async def save_webhook(_db: Any, **kwargs: Any) -> Any:
        return kwargs["webhook_id"]

    repository = make_repository(save_webhook=AsyncMock(side_effect=save_webhook))
    service = integrations.IntegrationService(cipher, b"s" * 32, repository=repository)
    db = SimpleNamespace()
    saved = await service.save_config(
        db,
        company_id=None,
        provider="mock_llm",
        credentials={},
        enabled=True,
    )
    assert saved["credentials_saved"] is True
    encrypted = repository.save_config.await_args.args[3]
    assert (
        cipher.decrypt_json(encrypted, associated_data=integrations.config_aad(None, "mock_llm"))
        == {}
    )
    disconnected = await service.save_config(
        db,
        company_id=None,
        provider="mock_llm",
        credentials=None,
        enabled=False,
        clear_credentials=True,
    )
    assert disconnected["credentials_saved"] is False
    with pytest.raises(HTTPException):
        await service.save_config(
            db,
            company_id=None,
            provider="mock_llm",
            credentials={},
            enabled=True,
            clear_credentials=True,
        )

    webhook = await service.create_webhook(
        db,
        company_id=None,
        url="https://hooks.example.test/events",
        event_types={"complaint.status_changed"},
    )
    hook_id = webhook["id"]
    encrypted_secret = repository.save_webhook.await_args.kwargs["ciphertext"]
    assert (
        cipher.decrypt(
            encrypted_secret, associated_data=integrations.webhook_secret_aad(hook_id)
        ).decode()
        == webhook["signing_secret"]
    )
    repository.list_webhooks.return_value = [
        {
            "id": hook_id,
            "company_id": None,
            "url": webhook["url"],
            "event_types": ["complaint.status_changed"],
        }
    ]
    await service.deactivate_webhook(db, hook_id)
    assert repository.save_webhook.await_args.kwargs["active"] is False

    company_id = uuid4()
    config = {"verify_token": "verify-me"}
    repository.webhook_config.return_value = (
        company_id,
        cipher.encrypt_json(
            config, associated_data=integrations.config_aad(company_id, "whatsapp_cloud")
        ),
    )
    assert (
        await service.verify_whatsapp_subscription(
            db, company_id, "subscribe", "verify-me", "challenge-123"
        )
        == "challenge-123"
    )
    with pytest.raises(HTTPException):
        await service.verify_whatsapp_subscription(db, company_id, "subscribe", "bad", "x")

    key = await service.create_api_key(
        db,
        permissions={"complaints.view"},
        all_companies=True,
        company_ids=[],
        expires_at=None,
    )
    assert key["token"].startswith("ipk_")
    key_hash = repository.create_api_key.await_args.kwargs["key_hash"]
    assert len(key_hash) == 32
    assert await service.list_api_keys(db) == []
    assert await service.revoke_api_key(db, uuid4())
    assert await service.status(db, uuid4())
    assert await service.dead_letters(db) == []
    await service.requeue_dead_letter(db, uuid4())
