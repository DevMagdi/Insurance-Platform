"""Mocked provider, privacy, webhook and worker coverage for Phase 4."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.domain.schemas import WhatsAppSendRequest
from app.security.blobs import RECORDING, BlobCipher
from app.security.integration_crypto import SecretCipher
from app.services import integration_providers, integrations
from app.services.integration_providers import (
    EmailSendRequest,
    IMAPMailProvider,
    IntegrationProviderError,
    MockSpeechProvider,
    MockVoiceProvider,
    OpenAICompatibleProvider,
    OutboundAttachment,
    ProviderResult,
    SendGridEmailProvider,
    SESEmailProvider,
    SignedWebhookProvider,
    SMTPEmailProvider,
    TwilioVoiceProvider,
    VoiceCallRequest,
    VonageVoiceProvider,
    WhatsAppCloudProvider,
)
from app.services.integration_providers import (
    WhatsAppSendRequest as ProviderWhatsAppSendRequest,
)
from app.worker import _process_integration_job


@pytest.fixture
def cipher() -> SecretCipher:
    return SecretCipher(b"p" * 32)


def install_httpx_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda request: _capture(handler, requests, request))
    original_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(integration_providers.httpx, "AsyncClient", client_factory)
    return requests


def _capture(handler: Any, requests: list[httpx.Request], request: httpx.Request) -> httpx.Response:
    requests.append(request)
    return handler(request)


def test_secret_cipher_is_context_bound_and_randomized(cipher: SecretCipher) -> None:
    plaintext = {"api_key": "private"}
    one = cipher.encrypt_json(plaintext, associated_data="provider:one")
    two = cipher.encrypt_json(plaintext, associated_data="provider:one")
    assert one != two
    assert cipher.decrypt_json(one, associated_data="provider:one") == plaintext
    with pytest.raises(ValueError, match="failed authentication"):
        cipher.decrypt_json(one, associated_data="provider:two")


def test_pii_minimization_masks_egyptian_identifiers_and_known_names() -> None:
    source = (
        "Caller Ahmed Hassan; national ID 29876543210987; "
        "email ahmed@example.test; mobile 01012345678; policy POL-843292."
    )
    minimized = integrations.minimize_pii(source, known_names=("Ahmed Hassan",))
    for sensitive in ("Ahmed Hassan", "29876543210987", "ahmed@example.test", "01012345678"):
        assert sensitive not in minimized
    assert "[NATIONAL_ID]" in minimized
    assert "[EMAIL]" in minimized
    assert "[PHONE]" in minimized
    assert "[POLICY_REFERENCE]" in minimized
    assert integrations.extract_ticket_no("Please check CMP-2026-001245") == "CMP-2026-001245"


def test_timestamped_hmac_rejects_tampering_and_stale_events() -> None:
    timestamp = str(int(datetime.now(UTC).timestamp()))
    body = b'{"event_id":"mail-1"}'
    secret = "test-signing-key"
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    now = int(datetime.now(UTC).timestamp())
    assert integrations.verify_timestamped_hmac(body, timestamp, signature, secret, now=now)
    assert not integrations.verify_timestamped_hmac(
        body + b"!", timestamp, signature, secret, now=now
    )
    assert not integrations.verify_timestamped_hmac(body, "1", signature, secret, now=now)


def test_email_delivery_webhooks_normalize_delivered_and_bounce_events(
    cipher: SecretCipher,
) -> None:
    service = integrations.IntegrationService(cipher, b"e" * 32)
    events = service._normalize_webhook(
        "email_webhook",
        [
            {
                "sg_event_id": "sg-event-1",
                "sg_message_id": "provider-message-1.filter0001",
                "event": "delivered",
            },
            {
                "sg_event_id": "sg-event-2",
                "sg_message_id": "provider-message-2",
                "event": "bounce",
            },
        ],
    )
    assert [event[0] for event in events] == ["email_status", "email_status"]
    assert events[0][2] == {"provider_message_id": "provider-message-1", "status": "delivered"}
    assert events[1][2]["status"] == "failed"


async def test_smtp_ses_and_imap_adapters_use_mocked_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smtp_send = AsyncMock()
    monkeypatch.setattr(integration_providers.aiosmtplib, "send", smtp_send)
    mail = EmailSendRequest(
        recipients=("claims@example.test",),
        subject="Complaint CMP-2026-000001",
        text_body="Follow-up",
        attachments=(OutboundAttachment("proof.txt", "text/plain", b"proof"),),
    )
    smtp_result = await SMTPEmailProvider(
        {"host": "mail.example.test", "from_email": "agent@example.test"}
    ).send(mail, idempotency_key="smtp-idempotency")
    assert smtp_result.provider_id
    assert smtp_send.await_args.kwargs["hostname"] == "mail.example.test"
    assert "proof.txt" in smtp_send.await_args.args[0].as_string()
    with pytest.raises(IntegrationProviderError, match="email_configuration_missing"):
        await SMTPEmailProvider({}).send(mail, idempotency_key="smtp-idempotency")
    with pytest.raises(IntegrationProviderError, match="attachment_mime_invalid"):
        await SMTPEmailProvider({"host": "mail", "from_email": "a@example.test"}).send(
            EmailSendRequest(
                ("a@example.test",),
                "Subject",
                "Body",
                attachments=(OutboundAttachment("bad.bin", "invalid", b"bad"),),
            ),
            idempotency_key="smtp-idempotency",
        )

    smtp_send.reset_mock()
    with pytest.raises(IntegrationProviderError, match="ses_configuration_missing"):
        await SESEmailProvider({}).send(mail, idempotency_key="ses-idempotency")
    await SESEmailProvider(
        {
            "from_email": "agent@example.test",
            "region": "eu-west-1",
            "smtp_username": "ses-user",
            "smtp_password": "ses-password",
        }
    ).send(mail, idempotency_key="ses-idempotency")
    # SES is reached over its regional SMTP endpoint; no AWS SDK is involved.
    assert smtp_send.await_args.kwargs["hostname"] == "email-smtp.eu-west-1.amazonaws.com"
    assert smtp_send.await_args.kwargs["username"] == "ses-user"
    assert smtp_send.await_args.kwargs["start_tls"] is True
    assert "proof.txt" in smtp_send.await_args.args[0].as_string()

    class IMAP:
        def select(self, *_args: Any, **_kwargs: Any) -> tuple[str, list[bytes]]:
            return "OK", [b"2"]

        def search(self, *_args: Any) -> tuple[str, list[bytes]]:
            return "OK", [b"1 2"]

        def fetch(self, sequence: bytes, *_args: Any) -> tuple[str, list[tuple[bytes, bytes]]]:
            message_id = b"Message-ID: <mail-one@example.test>\r\n" if sequence == b"1" else b""
            raw = (
                b"From: Customer <customer@example.test>\r\n"
                b"Subject: Re: CMP-2026-000001\r\n"
                + message_id
                + b"Content-Type: text/plain; charset=utf-8\r\n\r\nTicket update\n"
            )
            return "OK", [(b"1 (RFC822)", raw)]

        def store(self, *_args: Any) -> tuple[str, list[bytes]]:
            return "OK", [b"stored"]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", []

    imap = IMAP()
    inbox = IMAPMailProvider({"host": "imap.example.test"})
    inbox._connect = lambda: imap  # type: ignore[method-assign]
    unseen = await inbox.fetch_unseen(limit=5)
    assert len(unseen) == 2
    assert unseen[0].external_message_id == "mail-one@example.test"
    assert unseen[0].sender == "customer@example.test"
    assert "Ticket update" in unseen[0].body
    assert unseen[1].external_message_id.startswith("sha256:")
    await inbox.mark_seen("2")
    with pytest.raises(IntegrationProviderError, match="imap_sequence_invalid"):
        await inbox.mark_seen("bad\\r\\n1")


async def test_signed_webhook_adapter_signs_payload_and_rejects_non_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = [204]
    requests = install_httpx_mock(monkeypatch, lambda _r: httpx.Response(statuses.pop(0)))
    await SignedWebhookProvider().send(
        "https://hooks.example.test/events", "secret", b'{"event":"test"}', timestamp=123
    )
    signature = requests[0].headers["X-Insurance-Signature"].removeprefix("sha256=")
    expected = hmac.new(b"secret", b'123.{"event":"test"}', hashlib.sha256).hexdigest()
    assert signature == expected
    statuses.append(503)
    with pytest.raises(IntegrationProviderError) as retryable:
        await SignedWebhookProvider().send(
            "https://hooks.example.test/events", "secret", b"{}", timestamp=1
        )
    assert retryable.value.retryable is True


def test_normalize_whatsapp_and_voice_callbacks_and_reject_malformed_events(
    cipher: SecretCipher,
) -> None:
    service = integrations.IntegrationService(cipher, b"e" * 32)
    whatsapp = service._normalize_webhook(
        "whatsapp_cloud",
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid-inbound-1",
                                        "from": "201012345678",
                                        "text": {"body": "Please update CMP-2026-000001"},
                                    }
                                ],
                                "statuses": [
                                    {"id": "wamid-outbound-1", "status": "read", "timestamp": "1"}
                                ],
                            }
                        }
                    ]
                }
            ]
        },
    )
    assert [event[0] for event in whatsapp] == ["inbound_whatsapp", "whatsapp_status"]
    assert whatsapp[0][2]["sender"] == "+201012345678"
    assert whatsapp[1][2]["status"] == "read"
    voice_status = service._normalize_webhook(
        "twilio", {"CallSid": "CA123", "CallStatus": "completed"}
    )
    assert voice_status[0][0] == "voice_status"
    inbound_voice = service._normalize_webhook(
        "vonage",
        {
            "uuid": "call-123",
            "ticket_no": "CMP-2026-000002",
            "caller": {"number": "+201012345678"},
            "consent_confirmed": True,
            "request_human": True,
            "transcript": "Please help",
        },
    )
    assert inbound_voice[0][0] == "inbound_voice"
    assert inbound_voice[0][2]["caller"] == "+201012345678"
    with pytest.raises(HTTPException) as malformed:
        service._normalize_webhook("whatsapp_cloud", {"entry": [{"changes": [None]}]})
    assert malformed.value.status_code == 422


async def test_mock_voice_and_speech_providers_cover_consent_and_human_fallback() -> None:
    voice = MockVoiceProvider()
    call = VoiceCallRequest("+201012345678", "CMP-2026-000001", "Hello", "en", False)
    denied = await voice.follow_up(call, idempotency_key="denied-key")
    assert denied.outcome == "human_fallback_no_consent"
    inbound = await voice.answer_inbound(
        {"provider_call_id": "mock-123", "consent_confirmed": True, "request_human": True}
    )
    assert inbound.outcome == "human_handoff"
    speech = MockSpeechProvider()
    assert "Mock transcription" in await speech.transcribe(b"ignored audio", language="en")
    assert await speech.synthesize("Hello", language="en") == b"mock-audio:en:Hello"
    assert "human agent" in await speech.respond("status", language="en")


async def test_sendgrid_adapter_uses_mock_http_and_encoded_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = install_httpx_mock(
        monkeypatch, lambda _: httpx.Response(202, headers={"X-Message-Id": "sg-123"})
    )
    provider = SendGridEmailProvider({"api_key": "secret", "from_email": "claims@example.test"})
    result = await provider.send(
        EmailSendRequest(
            ("insurer@example.test",),
            "Complaint CMP-2026-000001",
            "Please review.",
            attachments=(OutboundAttachment("evidence.txt", "text/plain", b"safe evidence"),),
        ),
        idempotency_key="email-job-123",
    )
    assert result.provider_id == "sg-123"
    request = requests[0]
    assert request.headers["Authorization"] == "Bearer secret"
    body = json.loads(request.content)
    assert body["attachments"][0]["content"] == base64.b64encode(b"safe evidence").decode()
    assert body["custom_args"]["idempotency_key"] == "email-job-123"


async def test_whatsapp_cloud_adapter_sends_template_through_mock_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = install_httpx_mock(
        monkeypatch, lambda _: httpx.Response(200, json={"messages": [{"id": "wamid.42"}]})
    )
    provider = WhatsAppCloudProvider(
        {"access_token": "test-token", "phone_number_id": "1234", "api_version": "v21.0"}
    )
    result = await provider.send(
        ProviderWhatsAppSendRequest(
            "+201012345678", "Your complaint is in review", "ticket_update", "en", ("CMP-2026-1",)
        ),
        idempotency_key="wa-job-123",
    )
    assert result.provider_id == "wamid.42"
    assert requests[0].headers["Authorization"] == "Bearer test-token"
    payload = json.loads(requests[0].content)
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "ticket_update"


async def test_openai_compatible_llm_adapter_parses_structured_insights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "classification": {"category": "claims", "priority": "high"},
                            "sentiment": {"sentiment": "concerned"},
                            "summary": {"summary": "Customer asks for an update."},
                            "reply": {"reply": "We will update you soon.", "locale": "en"},
                        }
                    )
                }
            }
        ],
        "model": "mock-llm-v1",
    }
    requests = install_httpx_mock(monkeypatch, lambda _: httpx.Response(200, json=response))
    provider = OpenAICompatibleProvider(
        {"api_key": "llm-secret", "base_url": "https://llm.example.test/v1", "model": "mock"}
    )
    result = await provider.analyze("Need a claim update", locale="en")
    assert result.model == "mock"
    assert {item.kind for item in result.insights} == {
        "classification",
        "sentiment",
        "summary",
        "reply",
    }
    assert requests[0].headers["Authorization"] == "Bearer llm-secret"


async def test_twilio_voice_adapter_uses_mock_http_and_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = install_httpx_mock(monkeypatch, lambda _: httpx.Response(201, json={"sid": "CA123"}))
    provider = TwilioVoiceProvider(
        {
            "account_sid": "AC123",
            "auth_token": "twilio-secret",
            "from_number": "+12025550100",
            "status_callback_url": "https://hooks.example.test/twilio",
        }
    )
    result = await provider.follow_up(
        VoiceCallRequest("+201012345678", "CMP-2026-000001", "Follow-up", "en", True),
        idempotency_key="call-job-123",
    )
    assert result.provider_call_id == "CA123"
    assert requests[0].url.path.endswith("/Accounts/AC123/Calls.json")
    assert requests[0].headers["Idempotency-Key"] == "call-job-123"
    assert b"Follow-up" in requests[0].content
    blocked = await provider.follow_up(
        VoiceCallRequest("+201012345678", "CMP-2026-000001", "Follow-up", "en", False),
        idempotency_key="call-job-456",
    )
    assert blocked.outcome == "human_fallback_no_consent"
    assert len(requests) == 1


async def test_vonage_voice_adapter_signs_and_sends_mock_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_key = private_key.public_key()
    requests = install_httpx_mock(
        monkeypatch, lambda _: httpx.Response(201, json={"uuid": "vonage-call-1"})
    )
    provider = VonageVoiceProvider(
        {
            "application_id": "vonage-app",
            "private_key": pem,
            "from_number": "12025550100",
            "status_callback_url": "https://hooks.example.test/vonage",
        }
    )
    result = await provider.follow_up(
        VoiceCallRequest("+201012345678", "CMP-2026-000001", "Follow-up", "ar", True),
        idempotency_key="call-job-789",
    )
    assert result.provider_call_id == "vonage-call-1"
    token = requests[0].headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, public_key, algorithms=["RS256"])
    assert claims["iss"] == "vonage-app"
    assert json.loads(requests[0].content)["ncco"][0]["voiceName"] == "Lea"


async def test_worker_dispatches_email_job_to_a_mock_provider(
    monkeypatch: pytest.MonkeyPatch, cipher: SecretCipher
) -> None:
    captured: dict[str, Any] = {}

    class MockEmailProvider:
        async def send(self, request: EmailSendRequest, *, idempotency_key: str) -> ProviderResult:
            captured["request"] = request
            captured["idempotency_key"] = idempotency_key
            return ProviderResult("mock-email-123")

    monkeypatch.setattr("app.worker._email_provider", lambda *_: MockEmailProvider())
    queries: list[tuple[str, dict[str, Any]]] = []

    async def query(_engine: Any, sql: str, params: dict[str, Any]) -> list[Any]:
        queries.append((sql, params))
        return []

    monkeypatch.setattr("app.worker._worker_query", query)
    job_id = uuid4()
    digest = b"d" * 32
    config = {"from_email": "sender@example.test"}
    payload = {
        "recipients": ["claims@example.test"],
        "subject": "CMP-2026-000001",
        "body": "A status update",
        "locale": "en",
        "attachment_ids": [],
    }
    row = SimpleNamespace(
        job_id=job_id,
        job_kind="send_email",
        idempotency_digest=digest,
        payload_ciphertext=cipher.encrypt_json(
            payload, associated_data=integrations.job_aad(digest)
        ),
        config_ciphertext=cipher.encrypt_json(
            config, associated_data=integrations.config_aad(None, "smtp")
        ),
        config_company_id=None,
        provider="smtp",
        attempt=1,
        recipient_mobile=None,
        call_log_id=None,
        webhook_id=None,
        webhook_secret_ciphertext=None,
        webhook_url=None,
    )
    await _process_integration_job(
        {"engine": object(), "integration_cipher": cipher, "blobs": BlobCipher(b"b" * 32)}, row
    )
    assert captured["request"].recipients == ("claims@example.test",)
    assert captured["idempotency_key"] == digest.hex()
    assert any("finish_integration_job" in sql for sql, _ in queries)
    assert queries[-1][1]["provider_id"] == "mock-email-123"


async def test_worker_minimizes_llm_output_before_persisting(
    monkeypatch: pytest.MonkeyPatch, cipher: SecretCipher
) -> None:
    completed: list[dict[str, Any]] = []

    async def query(_engine: Any, sql: str, params: dict[str, Any]) -> list[Any]:
        if "complete_ai_job" in sql:
            completed.append(json.loads(params["items"]))
        return []

    monkeypatch.setattr("app.worker._worker_query", query)
    job_id = uuid4()
    digest = b"a" * 32
    payload = {
        "text": "National ID 29876543210987; email alice@example.test.",
        "locale": "en",
    }
    row = SimpleNamespace(
        job_id=job_id,
        job_kind="ai_analyze",
        idempotency_digest=digest,
        payload_ciphertext=cipher.encrypt_json(
            payload, associated_data=integrations.job_aad(digest)
        ),
        config_ciphertext=cipher.encrypt_json(
            {}, associated_data=integrations.config_aad(None, "mock_llm")
        ),
        config_company_id=None,
        provider="mock_llm",
        attempt=1,
        recipient_mobile=None,
        call_log_id=None,
        webhook_id=None,
        webhook_secret_ciphertext=None,
        webhook_url=None,
    )
    await _process_integration_job({"engine": object(), "integration_cipher": cipher}, row)
    assert completed
    stored = [
        cipher.decrypt_json(
            base64.b64decode(item["ciphertext"]),
            associated_data=integrations.ai_insight_aad(job_id, item["kind"]),
        )
        for item in completed[0]
    ]
    combined = json.dumps(stored)
    assert "29876543210987" not in combined
    assert "alice@example.test" not in combined
    assert "[NATIONAL_ID]" in combined
    assert "[EMAIL]" in combined


async def test_whatsapp_queue_records_staff_sender_and_rejects_nonstaff(
    cipher: SecretCipher,
) -> None:
    staff_id = uuid4()
    complaint_id = uuid4()
    company_id = uuid4()
    customer_id = uuid4()
    created_at = datetime.now(UTC)
    complaint = SimpleNamespace(
        id=complaint_id,
        company_id=company_id,
        customer_id=customer_id,
        created_at=created_at,
        ticket_no="CMP-2026-000001",
        status=SimpleNamespace(value="in_review"),
    )

    class ComplaintRepo:
        async def get(self, *_args: Any) -> Any:
            return complaint

    class IntegrationRepo:
        def __init__(self) -> None:
            self.enqueue_job = _async_result(
                {"job_id": uuid4(), "state": "queued", "created": True}
            )
            self.webhooks_for_event = _async_result([])

    repository = IntegrationRepo()
    service = integrations.IntegrationService(
        cipher, b"i" * 32, repository=repository, complaints=ComplaintRepo()
    )
    contact = SimpleNamespace(mobile="+201012345678", whatsapp_available=True)
    db = SimpleNamespace(
        execute=_async_result(SimpleNamespace(one_or_none=lambda: contact)),
        scalar=_async_result(None),
    )
    scope = AccessScope(
        actor=Actor(staff_id, "staff"),
        all_companies=True,
        ownership=OwnershipLevel.ALL,
    )
    await service.send_whatsapp(
        db,
        scope,
        complaint_id,
        WhatsAppSendRequest(body="Complaint update", template_name="ticket_update"),
        "idempotency-123",
    )
    assert repository.enqueue_job.await_args.kwargs["sender_staff_id"] == staff_id
    assert repository.enqueue_job.await_args.kwargs["job_kind"] == "send_whatsapp"

    customer_scope = AccessScope.for_customer(Actor(customer_id, "customer"))
    with pytest.raises(HTTPException) as error:
        await service.send_whatsapp(
            db,
            customer_scope,
            complaint_id,
            WhatsAppSendRequest(body="Unapproved customer send", template_name="ticket_update"),
            "idempotency-456",
        )
    assert error.value.status_code == 403


async def test_inbound_webhook_signature_queues_encrypted_ticket_matched_event(
    cipher: SecretCipher,
) -> None:
    company_id = uuid4()
    secret = "mail-webhook-secret"
    now = str(int(datetime.now(UTC).timestamp()))
    document = {
        "event_id": "mail-event-42",
        "from": "customer@example.test",
        "to": "support@example.test",
        "subject": "Re: CMP-2026-000001",
        "text": "Please update my claim.",
    }
    raw = json.dumps(document).encode()
    signature = hmac.new(secret.encode(), now.encode() + b"." + raw, hashlib.sha256).hexdigest()

    class WebhookRepo:
        async def webhook_config(self, *_args: Any) -> tuple[None, bytes]:
            return None, cipher.encrypt_json(
                {"webhook_secret": secret},
                associated_data=integrations.config_aad(None, "email_webhook"),
            )

        async def enqueue_inbound(
            self,
            _db: Any,
            _company: Any,
            _provider: str,
            _kind: str,
            ciphertext: bytes,
            idem: bytes,
            _digest: bytes,
        ) -> dict[str, Any]:
            self.idem = idem
            self.ciphertext = ciphertext
            return {"job_id": uuid4()}

    repository = WebhookRepo()
    service = integrations.IntegrationService(cipher, b"w" * 32, repository=repository)
    result = await service.receive_webhook(
        SimpleNamespace(),
        company_id=company_id,
        provider="email_webhook",
        raw_body=raw,
        headers={"x-webhook-timestamp": now, "x-webhook-signature": signature},
    )
    queued = cipher.decrypt_json(
        repository.ciphertext, associated_data=integrations.job_aad(repository.idem)
    )
    assert result["accepted"] == 1
    assert queued["ticket_no"] == "CMP-2026-000001"
    assert queued["body"] == "Please update my claim."


async def test_twilio_native_form_status_callback_is_signature_checked_and_queued(
    cipher: SecretCipher,
) -> None:
    company_id = uuid4()
    callback_url = "https://hooks.example.test/api/webhooks/twilio/" + str(company_id)
    auth_token = "twilio-auth-token"
    fields = {"CallSid": "CA123", "CallStatus": "completed", "Timestamp": "2026-10-16T10:00:00Z"}
    raw = urlencode(fields).encode()
    signed = callback_url + "".join(key + fields[key] for key in sorted(fields))
    signature = base64.b64encode(
        hmac.new(auth_token.encode(), signed.encode(), hashlib.sha1).digest()
    ).decode()

    class TwilioRepo:
        async def webhook_config(self, *_args: Any) -> tuple[Any, bytes]:
            config = {"auth_token": auth_token, "status_callback_url": callback_url}
            return company_id, cipher.encrypt_json(
                config, associated_data=integrations.config_aad(company_id, "twilio")
            )

        async def enqueue_inbound(
            self,
            _db: Any,
            _company: Any,
            _provider: str,
            kind: str,
            _ciphertext: bytes,
            _idem: bytes,
            _digest: bytes,
        ) -> dict[str, Any]:
            self.kind = kind
            return {"job_id": uuid4()}

    repository = TwilioRepo()
    service = integrations.IntegrationService(cipher, b"t" * 32, repository=repository)
    result = await service.receive_webhook(
        SimpleNamespace(),
        company_id=company_id,
        provider="twilio",
        raw_body=raw,
        headers={"x-twilio-signature": signature},
    )
    assert repository.kind == "voice_status"
    assert result["accepted"] == 1
    assert integrations.verify_twilio_signature(
        callback_url, {key: [value] for key, value in fields.items()}, signature, auth_token
    )
    assert not integrations.verify_twilio_signature(
        callback_url + "/tampered",
        {key: [value] for key, value in fields.items()},
        signature,
        auth_token,
    )


async def test_worker_encrypts_mock_voice_recording_and_transcript(
    monkeypatch: pytest.MonkeyPatch, cipher: SecretCipher
) -> None:
    call_id = uuid4()
    digest = b"v" * 32

    blobs = BlobCipher(b"b" * 32)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def query(_engine: Any, sql: str, params: dict[str, Any]) -> list[Any]:
        calls.append((sql, params))
        return []

    monkeypatch.setattr("app.worker._worker_query", query)
    row = SimpleNamespace(
        job_id=uuid4(),
        job_kind="voice_followup",
        idempotency_digest=digest,
        payload_ciphertext=cipher.encrypt_json(
            {
                "mobile": "+201012345678",
                "ticket_no": "CMP-2026-000001",
                "script": "A consented follow-up.",
                "language": "en",
                "consent_confirmed": True,
            },
            associated_data=integrations.job_aad(digest),
        ),
        config_ciphertext=cipher.encrypt_json(
            {}, associated_data=integrations.config_aad(None, "mock_voice")
        ),
        config_company_id=None,
        provider="mock_voice",
        attempt=1,
        recipient_mobile="+201012345678",
        company_id=uuid4(),
        call_log_id=call_id,
        webhook_id=None,
        webhook_secret_ciphertext=None,
        webhook_url=None,
    )
    await _process_integration_job(
        {"engine": object(), "integration_cipher": cipher, "blobs": blobs}, row
    )
    call = next(params for sql, params in calls if "create_call_job" in sql)
    assert call["provider_id"].startswith("mock-call-")
    transcript = cipher.decrypt(
        call["transcript"], associated_data=integrations.call_transcript_aad(call_id)
    ).decode()
    assert "Customer requested a status update" in transcript
    # The recording is an encrypted column value bound to the call, not an object key.
    assert "recording_key" not in call
    assert (
        blobs.decrypt(call["recording"], purpose=RECORDING, resource_id=call_id)
        == b"mock-audio-recording"
    )


def _async_result(value: Any) -> Any:
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=value)
