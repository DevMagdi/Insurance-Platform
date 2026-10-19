"""Connection tests, OAuth, sandbox sends and the real provider adapters.

Every transport is mocked: no test contacts Google, Microsoft, Meta, Twilio,
OpenAI or ClamAV, and no test carries real customer data. Contract tests pin
the exact HTTP shapes the documented provider APIs require.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.domain.schemas import (
    ConnectionTestRequest,
    OAuthStartRequest,
    SandboxSendRequest,
)
from app.security.integration_crypto import SecretCipher
from app.services import integration_providers
from app.services.integration_providers import (
    EmailSendRequest,
    GmailEmailProvider,
    IntegrationProviderError,
    MockLLMProvider,
    OAuthTokenBroker,
    OpenAISpeechProvider,
    OutlookEmailProvider,
    SMSProviderRequest,
    SMTPEmailProvider,
    TwilioSMSProvider,
    build_provider,
    probe_provider,
)
from app.services.integrations import IntegrationService, config_aad
from app.services.providers import ClamAVScanner, ScanTarget, UnconfiguredVirusScanner


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


def oauth_config(provider: str = "gmail_oauth") -> dict[str, Any]:
    return {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "from_email": "claims@example.test",
    }


# ---------------------------------------------------------------------------
# Provider contract tests
# ---------------------------------------------------------------------------


async def test_gmail_sends_raw_message_over_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            seen["token_grant"] = dict(httpx.QueryParams(request.content.decode()))
            return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600})
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "gmail-message-1"})

    install_httpx_mock(monkeypatch, handler)
    result = await GmailEmailProvider(oauth_config()).send(
        EmailSendRequest(
            recipients=("customer@example.test",), subject="CMP-2026-000001", text_body="Hello"
        ),
        idempotency_key="gmail-key-12345678",
    )
    assert result.provider_id == "gmail-message-1"
    assert seen["token_grant"]["grant_type"] == "refresh_token"
    assert seen["auth"] == "Bearer access-1"
    raw = base64.urlsafe_b64decode(seen["body"]["raw"])
    assert b"CMP-2026-000001" in raw and b"customer@example.test" in raw


async def test_gmail_connection_probe_never_sends_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "a", "expires_in": 60})
        return httpx.Response(200, json={"emailAddress": "claims@example.test"})

    install_httpx_mock(monkeypatch, handler)
    await GmailEmailProvider(oauth_config()).test_connection()
    assert not any(path.endswith("/messages/send") for path in paths)
    assert any(path.endswith("/profile") for path in paths)


async def test_outlook_graph_sendmail_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "ms-token", "expires_in": 3600})
        seen["url"] = str(request.url)
        from email import policy
        from email.parser import BytesParser

        seen["body"] = BytesParser(policy=policy.default).parsebytes(
            base64.b64decode(request.content)
        )
        assert request.headers["Content-Type"] == "text/plain"
        return httpx.Response(202)

    install_httpx_mock(monkeypatch, handler)
    result = await OutlookEmailProvider(
        {**oauth_config("outlook_oauth"), "tenant": "contoso.onmicrosoft.com"}
    ).send(
        EmailSendRequest(recipients=("customer@example.test",), subject="Update", text_body="Body"),
        idempotency_key="outlook-key-12345678",
    )
    assert result.provider_id is None
    assert seen["url"].endswith("graph.microsoft.com/v1.0/me/sendMail")
    assert seen["body"]["To"] == "customer@example.test"
    assert seen["body"].get_body(preferencelist=("plain",)).get_content().startswith("Body")
    assert "automated message" in seen["body"].get_body(preferencelist=("html",)).get_content()


async def test_twilio_sms_enforces_country_allowlist_and_status_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(201, json={"sid": "SM123"})

    install_httpx_mock(monkeypatch, handler)
    provider = TwilioSMSProvider(
        {
            "account_sid": "AC123",
            "auth_token": "token",
            "from_number": "+201000000001",
            "status_callback_url": "https://app.example.test/api/webhooks/twilio_sms/c1",
            "allowed_country_codes": "+20,+966",
        }
    )
    result = await provider.send(
        SMSProviderRequest(recipient="+201012345678", body="Your ticket was updated"),
        idempotency_key="sms-key-12345678",
    )
    assert result.provider_id == "SM123"
    assert seen["form"]["StatusCallback"].startswith("https://app.example.test/")
    with pytest.raises(IntegrationProviderError) as blocked:
        await provider.send(
            SMSProviderRequest(recipient="+15551234567", body="hello"),
            idempotency_key="sms-key-87654321",
        )
    assert blocked.value.code == "sms_destination_country_not_allowed"
    missing_allowlist = TwilioSMSProvider(
        {"account_sid": "AC123", "auth_token": "t", "from_number": "+201000000001"}
    )
    with pytest.raises(IntegrationProviderError) as refused:
        await missing_allowlist.send(
            SMSProviderRequest(recipient="+201012345678", body="hi"),
            idempotency_key="sms-key-11223344",
        )
    assert refused.value.code == "sms_country_allowlist_missing"


async def test_openai_budget_bounds_and_chat_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["timeout_used"] = True
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "classification": {"category": "claims", "priority": "normal"},
                                    "summary": {"summary": "Short"},
                                }
                            )
                        }
                    }
                ]
            },
        )

    install_httpx_mock(monkeypatch, handler)
    from app.services.integration_providers import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        {
            "api_key": "sk-test",
            "model": "gpt-4o-mini",
            "timeout_seconds": "5",
            "max_retries": "0",
            "max_tokens": "256",
        }
    )
    analysis = await provider.analyze("Complaint about a delayed claim", locale="en")
    assert analysis.model == "gpt-4o-mini"
    assert captured["body"]["max_tokens"] == 256
    assert captured["body"]["temperature"] == 0


async def test_openai_speech_transcription_and_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/transcriptions"):
            return httpx.Response(200, json={"text": "مرحبا"})
        return httpx.Response(200, content=b"mp3-bytes")

    install_httpx_mock(monkeypatch, handler)
    speech = OpenAISpeechProvider({"api_key": "sk-test"})
    text = await speech.transcribe(b"audio-bytes", language="ar")
    assert text == "مرحبا"
    audio = await speech.synthesize("Hello", language="en")
    assert audio == b"mp3-bytes"
    assert any(b"model" in request.content for request in captured)


async def test_oauth_token_broker_exchange_and_state_urls() -> None:
    config = oauth_config()
    assert "accounts.google.com" in OAuthTokenBroker.authorize_url(
        "gmail_oauth", config, "https://app.example.test/cb", "state-token"
    )
    assert "state=state-token" in OAuthTokenBroker.authorize_url(
        "gmail_oauth", config, "https://app.example.test/cb", "state-token"
    )
    assert "login.microsoftonline.com" in OAuthTokenBroker.authorize_url(
        "outlook_oauth", {**oauth_config("outlook_oauth")}, "https://app.example.test/cb", "s"
    )


async def test_clamav_scanner_verdicts_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def make_stream(reply: bytes) -> tuple[Any, Any]:
        reader = SimpleNamespace(readuntil=AsyncMock(return_value=reply))
        writer = SimpleNamespace(
            write=MagicMock(),
            drain=AsyncMock(),
            close=MagicMock(),
            wait_closed=AsyncMock(),
        )
        return reader, writer

    target = ScanTarget(
        attachment_id=uuid4(),
        original_filename="proof.pdf",
        mime_type="application/pdf",
        size_bytes=4,
        checksum_sha256="0" * 64,
        content=b"file",
    )
    scanner = ClamAVScanner({"host": "clamav.example.test", "port": "3310"})

    async def clean_stream(host: str, port: int) -> Any:
        return make_stream(b"stream: OK\x00")

    monkeypatch.setattr("app.services.providers.asyncio.open_connection", clean_stream)
    assert await scanner.scan(target) == "clean"

    async def pong_stream(host: str, port: int) -> Any:
        return make_stream(b"PONG\x00")

    monkeypatch.setattr("app.services.providers.asyncio.open_connection", pong_stream)
    assert await scanner.ping() == "PONG"

    async def infected_stream(host: str, port: int) -> Any:
        return make_stream(b"stream: Eicar-Test-Signature FOUND\x00")

    monkeypatch.setattr("app.services.providers.asyncio.open_connection", infected_stream)
    assert await scanner.scan(target) == "infected"

    # An unreachable daemon must NOT be reported as clean.
    async def boom(host: str, port: int) -> Any:
        raise OSError("down")

    monkeypatch.setattr("app.services.providers.asyncio.open_connection", boom)
    assert await scanner.scan(target) is None
    assert await UnconfiguredVirusScanner().scan(target) is None
    with pytest.raises(IntegrationProviderError):
        await probe_provider("clamav", {"host": "clamav.example.test"})


async def test_probe_registry_rejects_unknown_and_mock_guards() -> None:
    with pytest.raises(IntegrationProviderError) as unsupported:
        await probe_provider("not_real", {})
    assert unsupported.value.code == "provider_connection_test_unsupported"
    assert isinstance(build_provider("mock_llm", {}), MockLLMProvider)
    with pytest.raises(IntegrationProviderError):
        build_provider("not_real", {})


# ---------------------------------------------------------------------------
# IntegrationService: encryption, merge, statuses, sandbox, OAuth
# ---------------------------------------------------------------------------


class FakeRepository:
    def __init__(self) -> None:
        self.stored: dict[tuple[Any, str], dict[str, Any]] = {}
        self.test_records: list[tuple[str, str, str | None]] = []
        self.saved: list[dict[str, Any]] = []

    async def save_config(
        self,
        db: Any,
        company_id: Any,
        provider: str,
        ciphertext: bytes | None,
        enabled: bool,
        clear_credentials: bool = False,
    ) -> Any:
        key = (company_id, provider)
        prior = self.stored.get(key, {})
        if clear_credentials:
            self.stored[key] = {"ciphertext": None, "enabled": False}
        else:
            self.stored[key] = {
                "ciphertext": ciphertext if ciphertext is not None else prior.get("ciphertext"),
                "enabled": enabled,
            }
        self.saved.append(
            {
                "company_id": company_id,
                "provider": provider,
                "has_ciphertext": self.stored[key]["ciphertext"] is not None,
                "enabled": enabled,
            }
        )
        return uuid4()

    async def integration_credentials(
        self, db: Any, company_id: Any, provider: str, *, fallback: bool
    ) -> dict[str, Any] | None:
        row = self.stored.get((company_id, provider))
        if row is None and fallback and company_id is not None:
            row = self.stored.get((None, provider))
            company_id = None
        if row is None or row["ciphertext"] is None:
            return None
        return {
            "config_id": uuid4(),
            "config_company_id": company_id,
            "config_ciphertext": row["ciphertext"],
        }

    async def record_connection_test(
        self, db: Any, config_id: Any, status: str, error_code: str | None
    ) -> bool:
        self.test_records.append((str(config_id), status, error_code))
        return True

    async def integration_job_counts(
        self, db: Any, company_id: Any, kind: str, window_seconds: int = 86400
    ) -> int:
        return 0


def make_service() -> tuple[IntegrationService, FakeRepository]:
    repository = FakeRepository()
    service = IntegrationService(
        SecretCipher(b"s" * 32),
        b"i" * 32,
        repository=repository,
        oauth_redirect_base="https://app.example.test",
    )
    return service, repository


def staff_scope(actor_id: Any = None) -> SimpleNamespace:
    return SimpleNamespace(actor=SimpleNamespace(id=actor_id or uuid4(), scope="staff"))


async def test_save_config_merges_partial_updates_and_never_returns_secrets() -> None:
    service, repository = make_service()
    saved = await service.save_config(
        SimpleNamespace(),
        company_id=None,
        provider="smtp",
        credentials={"host": "mail.example.test", "from_email": "ops@example.test"},
        enabled=False,
    )
    assert saved["credentials_saved"] is True
    assert saved["connection_status"] == "configured"
    assert "password" not in json.dumps(saved, default=str)

    # A partial update keeps the previously stored secret.
    await service.save_config(
        SimpleNamespace(),
        company_id=None,
        provider="smtp",
        credentials={"password": "private-value", "host": "mail2.example.test"},
        enabled=True,
    )
    row = repository.stored[(None, "smtp")]
    decrypted = service.cipher.decrypt_json(
        row["ciphertext"], associated_data=config_aad(None, "smtp")
    )
    assert decrypted == {
        "host": "mail2.example.test",
        "from_email": "ops@example.test",
        "password": "private-value",
    }

    # Enabling without the required fields fails closed.
    with pytest.raises(HTTPException) as incomplete:
        await service.save_config(
            SimpleNamespace(),
            company_id=None,
            provider="gmail_oauth",
            credentials={"client_id": "only-id"},
            enabled=True,
        )
    assert incomplete.value.status_code == 422

    # Clearing credentials disconnects and marks the row unconfigured.
    cleared = await service.save_config(
        SimpleNamespace(),
        company_id=None,
        provider="smtp",
        credentials=None,
        enabled=False,
        clear_credentials=True,
    )
    assert cleared["connection_status"] == "unconfigured"
    assert repository.stored[(None, "smtp")]["ciphertext"] is None


async def test_test_connection_records_status_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository = make_service()
    await service.save_config(
        SimpleNamespace(),
        company_id=None,
        provider="smtp",
        credentials={"host": "mail.example.test", "from_email": "ops@example.test"},
        enabled=True,
    )

    async def ok_probe(provider: str, config: dict[str, Any]) -> None:
        assert config["host"] == "mail.example.test"
        return None

    monkeypatch.setattr("app.services.integrations.probe_provider", ok_probe)
    result = await service.test_connection(
        SimpleNamespace(), scope=staff_scope(), company_id=None, provider="smtp"
    )
    assert result["connection_status"] == "connected"
    assert result["error_code"] is None
    assert repository.test_records[-1][1] == "connected"

    async def failing_probe(provider: str, config: dict[str, Any]) -> None:
        raise IntegrationProviderError("smtp_connection_failed")

    monkeypatch.setattr("app.services.integrations.probe_provider", failing_probe)
    failed = await service.test_connection(
        SimpleNamespace(), scope=staff_scope(), company_id=None, provider="smtp"
    )
    assert failed["connection_status"] == "failed"
    assert failed["error_code"] == "smtp_connection_failed"
    assert failed["message"]
    assert repository.test_records[-1][1] == "failed"

    missing = await service.test_connection(
        SimpleNamespace(), scope=staff_scope(), company_id=None, provider="whatsapp_cloud"
    )
    assert missing["connection_status"] == "unconfigured"


async def test_sandbox_send_requires_explicit_consent_and_masked_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository = make_service()
    await service.save_config(
        SimpleNamespace(),
        company_id=None,
        provider="twilio_sms",
        credentials={
            "account_sid": "AC123",
            "auth_token": "token",
            "from_number": "+201000000001",
            "sandbox_recipient": "+201012345678",
            "allowed_country_codes": "+20",
        },
        enabled=True,
    )
    with pytest.raises(HTTPException) as needs_consent:
        await service.sandbox_send(
            SimpleNamespace(),
            scope=staff_scope(),
            request=SandboxSendRequest(provider="twilio_sms", confirm=False),
        )
    assert needs_consent.value.status_code == 422

    sent: list[SMSProviderRequest] = []

    async def fake_send(self: Any, request: SMSProviderRequest, *, idempotency_key: str) -> Any:
        sent.append(request)
        return SimpleNamespace(provider_id="SM1")

    monkeypatch.setattr(TwilioSMSProvider, "send", fake_send)
    result = await service.sandbox_send(
        SimpleNamespace(),
        scope=staff_scope(),
        request=SandboxSendRequest(provider="twilio_sms", confirm=True),
    )
    assert result["queued"] is True
    assert result["recipient_masked"].endswith("5678")
    assert "+201012345678" not in json.dumps(result, default=str)
    assert sent[0].recipient == "+201012345678"
    assert repository.test_records[-1][1] == "connected"


async def test_oauth_state_is_bound_to_actor_and_rejects_tampering() -> None:
    service, _repository = make_service()
    actor = uuid4()
    state = service._sign_oauth_state(actor, None, "gmail_oauth", "/staff/operations")
    company, redirect = service._verify_oauth_state(state, actor_id=actor, provider="gmail_oauth")
    assert company is None and redirect == "/staff/operations"
    with pytest.raises(HTTPException):
        service._verify_oauth_state(state, actor_id=uuid4(), provider="gmail_oauth")
    with pytest.raises(HTTPException):
        service._verify_oauth_state(state + "x", actor_id=actor, provider="gmail_oauth")
    with pytest.raises(HTTPException):
        service._verify_oauth_state(state, actor_id=actor, provider="outlook_oauth")

    # An open redirect target is clamped to the staff console.
    forged_payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "actor": str(actor),
                "company": None,
                "provider": "gmail_oauth",
                "nonce": "n",
                "redirect": "//evil.example.test",
                "exp": int(datetime.now(UTC).timestamp()) + 60,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).decode()
    signature = hmac.new(
        b"i" * 32,
        base64.urlsafe_b64decode(forged_payload.encode()),
        hashlib.sha256,
    ).hexdigest()
    _company, redirect = service._verify_oauth_state(
        f"{forged_payload}.{signature}", actor_id=actor, provider="gmail_oauth"
    )
    assert redirect == "/staff/operations"


async def test_oauth_start_requires_client_credentials_and_builds_url() -> None:
    service, _repository = make_service()
    with pytest.raises(HTTPException) as missing_client:
        await service.oauth_start(
            SimpleNamespace(),
            scope=staff_scope(),
            request=OAuthStartRequest(provider="gmail_oauth"),
        )
    assert missing_client.value.status_code == 422
    await service.save_config(
        SimpleNamespace(),
        company_id=None,
        provider="gmail_oauth",
        credentials=oauth_config(),
        enabled=False,
    )
    started = await service.oauth_start(
        SimpleNamespace(),
        scope=staff_scope(),
        request=OAuthStartRequest(provider="gmail_oauth"),
    )
    assert "accounts.google.com" in started["authorization_url"]
    assert "state=" in started["authorization_url"]


async def test_ai_budget_rejects_over_the_daily_limit() -> None:
    service, repository = make_service()
    await service.save_config(
        SimpleNamespace(),
        company_id=None,
        provider="openai",
        credentials={"api_key": "sk-test", "daily_request_limit": "5"},
        enabled=True,
    )
    await service._enforce_ai_budget(SimpleNamespace(), uuid4())
    repository.integration_job_counts = AsyncMock(return_value=5)  # type: ignore[method-assign]
    with pytest.raises(HTTPException) as over_limit:
        await service._enforce_ai_budget(SimpleNamespace(), uuid4())
    assert over_limit.value.status_code == 429


def test_sms_schema_and_provider_name_catalog() -> None:
    from app.domain.schemas import IntegrationConfigSpec, IntegrationProviderName

    assert "gmail_oauth" in IntegrationProviderName.__args__
    assert "outlook_oauth" in IntegrationProviderName.__args__
    assert "twilio_sms" in IntegrationProviderName.__args__
    assert "clamav" in IntegrationProviderName.__args__
    with pytest.raises(ValidationError):
        IntegrationConfigSpec.model_validate({"provider": "email"})  # retired name

    probe = ConnectionTestRequest.model_validate({"provider": "openai_speech"})
    assert probe.provider == "openai_speech"


async def test_smtp_probe_failure_is_a_sanitized_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BoomSMTP:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def connect(self) -> None:
            raise OSError("refused")

    monkeypatch.setattr("app.services.integration_providers.aiosmtplib.SMTP", BoomSMTP)
    with pytest.raises(IntegrationProviderError) as failed:
        await SMTPEmailProvider(
            {"host": "mail.example.test", "from_email": "ops@example.test"}
        ).test_connection()
    assert failed.value.code == "smtp_connection_failed"
    assert failed.value.code.islower()


async def test_oauth_callback_exchanges_code_and_stores_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository = make_service()
    actor = uuid4()
    scope = staff_scope(actor)
    await service.save_config(
        SimpleNamespace(),
        company_id=None,
        provider="gmail_oauth",
        credentials=oauth_config(),
        enabled=False,
    )
    state = service._sign_oauth_state(actor, None, "gmail_oauth", "/staff/operations")

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "auth-code-1"
        return httpx.Response(
            200,
            json={"access_token": "a1", "refresh_token": "rotated-refresh", "expires_in": 3600},
        )

    install_httpx_mock(monkeypatch, handler)
    redirect, result = await service.oauth_callback(
        SimpleNamespace(), scope=scope, provider="gmail_oauth", code="auth-code-1", state=state
    )
    assert result["status"] == "configured"
    assert redirect.startswith("/staff/operations?")
    row = repository.stored[(None, "gmail_oauth")]
    stored = service.cipher.decrypt_json(
        row["ciphertext"], associated_data=config_aad(None, "gmail_oauth")
    )
    assert stored["refresh_token"] == "rotated-refresh"
    assert "access_token" not in stored


# ---------------------------------------------------------------------------
# API layer: delegation, permissions, and no credential echo
# ---------------------------------------------------------------------------


def api_request(service: Any) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                integrations=service,
                auth=SimpleNamespace(limits=None),
                factory=object(),
            )
        ),
        client=SimpleNamespace(host="192.0.2.10"),
    )


def api_context() -> SimpleNamespace:
    return SimpleNamespace(
        db=SimpleNamespace(),
        scope=staff_scope(),
        actor=SimpleNamespace(id=uuid4(), scope="staff"),
        permissions=frozenset({"integrations.manage"}),
    )


async def test_admin_connection_endpoints_delegate_and_hide_secrets() -> None:
    from fastapi import Response

    from app.api import admin_integrations

    service = SimpleNamespace(
        list_configs=AsyncMock(
            return_value=[
                {
                    "id": str(uuid4()),
                    "provider": "smtp",
                    "is_enabled": True,
                    "has_credentials": True,
                    "connection_status": "connected",
                    "last_test_at": "2026-10-17T10:00:00Z",
                    "last_error_code": None,
                }
            ]
        ),
        save_config=AsyncMock(
            return_value={
                "id": uuid4(),
                "provider": "smtp",
                "enabled": True,
                "credentials_saved": True,
                "connection_status": "configured",
            }
        ),
        test_connection=AsyncMock(
            return_value={
                "provider": "smtp",
                "company_id": None,
                "connection_status": "connected",
                "tested_at": None,
                "error_code": None,
                "message": "ok",
            }
        ),
        sandbox_send=AsyncMock(
            return_value={
                "provider": "smtp",
                "queued": True,
                "recipient_masked": "c***@example.test",
                "message": "sent",
            }
        ),
        oauth_start=AsyncMock(
            return_value={
                "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?x=1",
                "expires_at": datetime.now(UTC),
            }
        ),
    )
    request = api_request(service)
    ctx = api_context()
    rows = await admin_integrations.integrations(request, ctx)
    assert rows[0]["has_credentials"] is True
    assert "password" not in json.dumps(rows, default=str)

    await admin_integrations.configure_integration(
        SimpleNamespace(
            company_id=None,
            provider="smtp",
            enabled=True,
            clear_credentials=False,
            credentials={"host": "mail.example.test"},
        ),
        request,
        ctx,
        None,
    )
    service.save_config.assert_awaited_once()

    result = await admin_integrations.test_integration_connection(
        ConnectionTestRequest(provider="smtp"), request, Response(), ctx, None
    )
    assert result["connection_status"] == "connected"

    sandbox = await admin_integrations.sandbox_integration_send(
        SandboxSendRequest(provider="smtp", confirm=True), request, Response(), ctx, None
    )
    assert sandbox["recipient_masked"] == "c***@example.test"

    started = await admin_integrations.oauth_authorize(
        "gmail_oauth",
        OAuthStartRequest(provider="gmail_oauth"),
        request,
        Response(),
        ctx,
        None,
    )
    assert "accounts.google.com" in started["authorization_url"]
    assert service.oauth_start.await_count == 1
