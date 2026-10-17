"""Breadth contract tests for the remaining real provider adapters.

All transports are mocked (httpx.MockTransport, aiosmtplib, imaplib, JWT keys
generated in-memory). These tests pin the documented HTTP/protocol shapes of
SendGrid, SES, WhatsApp Cloud, IMAP, OAuth token exchange, Twilio Voice and
Vonage Voice without touching any external service.
"""

from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from typing import Any

import httpx
import pytest

from app.services import integration_providers
from app.services.integration_providers import (
    EmailSendRequest,
    IMAPMailProvider,
    IntegrationProviderError,
    MockSpeechProvider,
    MockVoiceProvider,
    OAuthTokenBroker,
    OutboundAttachment,
    SendGridEmailProvider,
    SESEmailProvider,
    TwilioVoiceProvider,
    VoiceCallRequest,
    VonageVoiceProvider,
    WhatsAppCloudProvider,
    WhatsAppSendRequest,
    build_provider,
    normalize_inbound_voice_event,
)


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


MAIL = EmailSendRequest(
    recipients=("customer@example.test",),
    subject="CMP-2026-000001",
    text_body="Hello",
    attachments=(OutboundAttachment("proof.txt", "text/plain", b"proof"),),
)


# ---------------------------------------------------------------------------
# SendGrid / SES
# ---------------------------------------------------------------------------


async def test_sendgrid_send_and_probe_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            return httpx.Response(202, headers={"X-Message-Id": "sg-1"})
        return httpx.Response(200, json={"scopes": ["mail.send"]})

    install_httpx_mock(monkeypatch, handler)
    provider = SendGridEmailProvider({"api_key": "SG.key", "from_email": "claims@example.test"})
    result = await provider.send(MAIL, idempotency_key="sg-key-12345678")
    assert result.provider_id == "sg-1"
    assert seen["body"]["custom_args"]["idempotency_key"] == "sg-key-12345678"
    assert seen["body"]["attachments"][0]["content"] == base64.b64encode(b"proof").decode()
    await provider.test_connection()
    assert seen["url"].endswith("/v3/scopes")

    with pytest.raises(IntegrationProviderError):
        await SendGridEmailProvider({}).send(MAIL, idempotency_key="k")


async def test_ses_send_and_probe_use_smtp_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[EmailMessage] = []

    async def fake_send(message: Any, **kwargs: Any) -> None:
        sent.append(message)

    monkeypatch.setattr(integration_providers.aiosmtplib, "send", fake_send)

    class FakeSMTP:
        def __init__(self, **kwargs: Any) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def login(self, username: str, password: str) -> None:
            assert username == "ses-user"

        async def noop(self) -> None:
            return None

        async def quit(self) -> None:
            return None

    monkeypatch.setattr(integration_providers.aiosmtplib, "SMTP", FakeSMTP)
    config = {
        "region": "eu-west-1",
        "from_email": "claims@example.test",
        "smtp_username": "ses-user",
        "smtp_password": "ses-pass",
    }
    provider = SESEmailProvider(config)
    result = await provider.send(MAIL, idempotency_key="ses-key-12345678")
    assert result.provider_id is None
    assert sent[0]["X-Idempotency-Key"] == "ses-key-12345678"
    await provider.test_connection()

    with pytest.raises(IntegrationProviderError):
        await SESEmailProvider({"from_email": "x@y.z"}).send(MAIL, idempotency_key="k")


# ---------------------------------------------------------------------------
# WhatsApp Cloud
# ---------------------------------------------------------------------------


async def test_whatsapp_template_text_and_probe_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"messages": [{"id": "wamid-1"}]})
        return httpx.Response(200, json={"id": "123", "verified_name": "Claims"})

    install_httpx_mock(monkeypatch, handler)
    provider = WhatsAppCloudProvider(
        {
            "phone_number_id": "PN1",
            "access_token": "EAAG-token",
            "api_version": "v21.0",
        }
    )
    result = await provider.send(
        WhatsAppSendRequest(
            recipient="+201012345678",
            template_name="complaint_update",
            parameters=("CMP-2026-000001",),
            language="ar",
            body="",
        ),
        idempotency_key="wa-key-12345678",
    )
    assert result.provider_id == "wamid-1"
    template = seen["body"]["template"]
    assert template["name"] == "complaint_update"
    assert template["components"][0]["parameters"][0]["text"] == "CMP-2026-000001"

    await provider.send(
        WhatsAppSendRequest(recipient="+201012345678", body="Hello", language="ar"),
        idempotency_key="wa-key-87654321",
    )
    assert seen["body"]["type"] == "text"
    assert seen["body"]["text"]["body"] == "Hello"

    await provider.test_connection()
    assert seen["method"] == "GET"
    assert "graph.facebook.com/v21.0/PN1" in seen["url"]

    with pytest.raises(IntegrationProviderError):
        await WhatsAppCloudProvider(
            {"phone_number_id": "PN1", "access_token": "t", "api_version": "bad"}
        ).send(
            WhatsAppSendRequest(recipient="+201012345678", body="x", language="ar"),
            idempotency_key="wa-key-11223344",
        )


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------


class FakeIMAP:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.stored: list[tuple[str, str, str]] = []

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b""]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        return "OK", [b"1"]

    def search(self, charset: Any, *criteria: Any) -> tuple[str, list[Any]]:
        return "OK", [b"1 2"]

    def fetch(self, sequence: bytes, query: str) -> tuple[str, list[Any]]:
        raw = (
            b"Message-ID: <msg-1@example.test>\r\n"
            b"From: Customer <customer@example.test>\r\n"
            b"Subject: Complaint CMP-2026-000123\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Please check CMP-2026-000123"
        )
        return "OK", [(b"1 (RFC822)", raw)]

    def store(self, sequence: str, command: str, flags: str) -> tuple[str, list[bytes]]:
        self.stored.append((sequence, command, flags))
        return "OK", [b""]

    def logout(self) -> tuple[str, list[bytes]]:
        return "OK", [b""]


async def test_imap_inbox_fetch_ack_and_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.integration_providers.imaplib.IMAP4_SSL", FakeIMAP)
    provider = IMAPMailProvider(
        {
            "host": "imap.example.test",
            "username": "inbox@example.test",
            "password": "secret",
            "mailbox": "INBOX",
        }
    )
    messages = await provider.fetch_unseen(limit=5)
    assert len(messages) == 2
    assert messages[0].external_message_id == "msg-1@example.test"
    assert messages[0].sender == "customer@example.test"
    assert "CMP-2026-000123" in messages[0].body

    await provider.mark_seen("1")
    await provider.test_connection()

    with pytest.raises(IntegrationProviderError):
        await provider.mark_seen("not-a-number")
    with pytest.raises(IntegrationProviderError):
        await IMAPMailProvider({"username": "u", "password": "p"}).fetch_unseen()


# ---------------------------------------------------------------------------
# OAuth token broker
# ---------------------------------------------------------------------------


async def test_oauth_broker_refresh_caches_and_exchange_stores_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
            },
        )

    install_httpx_mock(monkeypatch, handler)
    broker = OAuthTokenBroker(
        {
            "client_id": "cid",
            "client_secret": "cs",
            "refresh_token": "refresh-1",
        },
        provider="gmail_oauth",
    )
    first = await broker.access_token()
    second = await broker.access_token()
    assert first == second == "access-1"
    assert len(calls) == 1  # cached until expiry
    assert calls[0]["grant_type"] == "refresh_token"

    exchanged = await broker.exchange_code("auth-code", "https://app.example.test/cb")
    assert exchanged["refresh_token"] == "refresh-2"
    assert calls[-1]["grant_type"] == "authorization_code"

    with pytest.raises(IntegrationProviderError):
        await OAuthTokenBroker({"client_id": "only"}, provider="gmail_oauth").access_token()
    with pytest.raises(IntegrationProviderError):
        await OAuthTokenBroker(
            {"client_id": "c", "client_secret": "s", "refresh_token": "r", "tenant": "bad tenant!"},
            provider="outlook_oauth",
        ).access_token()


# ---------------------------------------------------------------------------
# Voice providers
# ---------------------------------------------------------------------------


async def test_twilio_voice_consent_gate_and_call_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = TwilioVoiceProvider(
        {
            "account_sid": "AC123",
            "auth_token": "token",
            "from_number": "+201000000001",
            "status_callback_url": "https://app.example.test/api/webhooks/voice_status",
        }
    )
    no_consent = await provider.follow_up(
        VoiceCallRequest("+201012345678", "CMP-2026-000001", "Script", "ar", False),
        idempotency_key="call-key-1",
    )
    assert no_consent.outcome == "human_fallback_no_consent"

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        if request.url.path.endswith(".json") and request.method == "POST":
            return httpx.Response(201, json={"sid": "CA123"})
        return httpx.Response(200, json={"status": "active"})

    install_httpx_mock(monkeypatch, handler)
    result = await provider.follow_up(
        VoiceCallRequest("+201012345678", "CMP-2026-000001", "Follow <script>", "ar", True),
        idempotency_key="call-key-12345678",
    )
    assert result.provider_call_id == "CA123"
    assert result.outcome == "initiated"
    assert "&lt;script&gt;" in seen["form"]["Twiml"]
    assert seen["form"]["StatusCallback"] == "https://app.example.test/api/webhooks/voice_status"

    await provider.test_connection()

    with pytest.raises(IntegrationProviderError):
        await TwilioVoiceProvider({}).follow_up(
            VoiceCallRequest("+201012345678", "CMP-2026-000001", "s", "ar", True),
            idempotency_key="k",
        )


def _rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


async def test_vonage_voice_jwt_and_call_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(IntegrationProviderError):
        VonageVoiceProvider({})._jwt()
    with pytest.raises(IntegrationProviderError):
        VonageVoiceProvider({"application_id": "app", "private_key": "not-a-key"})._jwt()

    provider = VonageVoiceProvider(
        {
            "application_id": "app-1",
            "private_key": _rsa_pem(),
            "from_number": "201000000001",
            "status_callback_url": "https://app.example.test/api/webhooks/voice_status",
        }
    )
    token = provider._jwt()
    assert token.count(".") == 2  # three compact JWS segments
    import jwt as pyjwt

    assert pyjwt.get_unverified_header(token)["kid"] == "app-1"

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"uuid": "v-1", "status": "started"})

    install_httpx_mock(monkeypatch, handler)
    result = await provider.follow_up(
        VoiceCallRequest("+201012345678", "CMP-2026-000001", "Script", "ar", True),
        idempotency_key="vg-key-12345678",
    )
    assert result.provider_call_id == "v-1"
    assert seen["auth"].startswith("Bearer ey")  # fresh jti per request
    assert seen["body"]["to"][0]["number"] == "201012345678"  # Vonage E.164 without +


async def test_inbound_voice_normalization_and_mocks() -> None:
    denied = normalize_inbound_voice_event({"call_id": "c1"})
    assert denied.outcome == "human_fallback_no_consent"
    completed = normalize_inbound_voice_event(
        {"call_id": "c2", "consent_confirmed": True, "text": "مرحبا"}
    )
    assert completed.transcript == "مرحبا" and completed.outcome == "completed"
    handoff = normalize_inbound_voice_event(
        {"call_id": "c3", "consent_confirmed": True, "text": "x", "request_human": True}
    )
    assert handoff.outcome == "human_handoff"

    voice = MockVoiceProvider()
    result = await voice.follow_up(
        VoiceCallRequest("+201012345678", "CMP-2026-000001", "s", "ar", True),
        idempotency_key="mock-key-12345678",
    )
    assert result.outcome == "completed"
    speech = MockSpeechProvider()
    assert await speech.transcribe(b"a", language="ar")
    assert await speech.synthesize("t", language="ar")
    assert await speech.respond("p", language="ar")
    await speech.test_connection()


# ---------------------------------------------------------------------------
# Registry breadth
# ---------------------------------------------------------------------------


def test_build_provider_covers_every_registered_adapter() -> None:
    from app.services.integration_providers import CONNECTION_TESTERS

    for name, cls in CONNECTION_TESTERS.items():
        if name in {"email_webhook", "imap"}:
            continue  # probe/worker-only adapters, built through their own registries
        assert isinstance(build_provider(name, {"api_key": "k"}), cls)


async def test_openai_budget_clamps_and_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.integration_providers import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        {
            "api_key": "sk",
            "timeout_seconds": "9999",
            "max_retries": "99",
            "max_tokens": "999999",
        }
    )
    timeout, retries, tokens = provider._budget()
    assert timeout <= 120 and retries <= 5 and tokens <= 4096

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})

    install_httpx_mock(monkeypatch, handler)
    with pytest.raises(IntegrationProviderError):
        await provider.analyze("text", locale="en")
