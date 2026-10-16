"""Additional mocked worker dispatch and IMAP-ingest coverage for Phase 4."""

from __future__ import annotations

import base64
import json
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from app import worker
from app.security.integration_crypto import SecretCipher
from app.services.integration_providers import (
    IncomingEmail,
    ProviderResult,
    WhatsAppSendRequest,
)
from app.services.integrations import call_recording_aad, call_transcript_aad, config_aad, job_aad
from app.worker import (
    _minimize_ai_value,
    _process_integration_job,
    poll_imap_inboxes,
    process_integration_jobs,
)


@pytest.fixture
def cipher() -> SecretCipher:
    return SecretCipher(b"w" * 32)


def make_job(
    cipher: SecretCipher, kind: str, payload: dict[str, Any], **overrides: Any
) -> SimpleNamespace:
    digest = b"d" * 32
    values: dict[str, Any] = {
        "job_id": uuid4(),
        "job_kind": kind,
        "idempotency_digest": digest,
        "payload_ciphertext": cipher.encrypt_json(payload, associated_data=job_aad(digest)),
        "attempt": 1,
        "provider": "mock_voice",
        "config_ciphertext": None,
        "config_company_id": None,
        "webhook_id": None,
        "webhook_secret_ciphertext": None,
        "webhook_url": None,
        "company_id": uuid4(),
        "recipient_mobile": "+201012345678",
        "call_log_id": uuid4(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_worker_rechecks_whatsapp_window_and_finishes_sent_or_dead_letter(
    monkeypatch: pytest.MonkeyPatch, cipher: SecretCipher
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    session_open = True

    async def query(_engine: Any, sql: str, params: dict[str, Any]) -> list[Any]:
        calls.append((sql, params))
        if "whatsapp_session_open" in sql:
            return [SimpleNamespace(open=session_open)]
        return []

    class WhatsApp:
        async def send(
            self, request: WhatsAppSendRequest, *, idempotency_key: str
        ) -> ProviderResult:
            assert request.recipient == "+201012345678"
            assert request.template_name is None
            assert idempotency_key == (b"d" * 32).hex()
            return ProviderResult("wamid-provider-1")

    monkeypatch.setattr(worker, "_worker_query", query)
    monkeypatch.setattr(worker, "WhatsAppCloudProvider", lambda _config: WhatsApp())
    row = make_job(
        cipher,
        "send_whatsapp",
        {"body": "A status update", "template_name": None, "language": "en"},
        provider="whatsapp_cloud",
    )
    ctx = {"engine": object(), "integration_cipher": cipher}
    await _process_integration_job(ctx, row)
    finish = calls[-1][1]
    assert finish["state"] == "sent"
    assert finish["provider_id"] == "wamid-provider-1"

    calls.clear()
    session_open = False
    await _process_integration_job(ctx, row)
    assert calls[-1][1]["state"] == "permanent"
    assert calls[-1][1]["error"] == "whatsapp_session_expired"


async def test_worker_persists_minimized_inbound_voice_transcript_and_encrypted_recording(
    monkeypatch: pytest.MonkeyPatch, cipher: SecretCipher
) -> None:
    queries: list[tuple[str, dict[str, Any]]] = []

    async def query(_engine: Any, sql: str, params: dict[str, Any]) -> list[Any]:
        queries.append((sql, params))
        return []

    class Storage:
        def __init__(self) -> None:
            self.object: tuple[str, bytes, str] | None = None

        async def put(self, key: str, stream: BytesIO, content_type: str) -> None:
            self.object = (key, stream.read(), content_type)

        async def get(self, _key: str, _max_bytes: int) -> bytes:
            assert self.object is not None
            return self.object[1]

    storage = Storage()
    row = make_job(
        cipher,
        "inbound_voice",
        {
            "provider_call_id": "provider-inbound-123",
            "ticket_no": "CMP-2026-000001",
            "consent_confirmed": True,
            "request_human": True,
            "text": "Customer 29876543210987 asked for a human agent.",
        },
        provider="twilio",
    )
    monkeypatch.setattr(worker, "_worker_query", query)
    await _process_integration_job(
        {"engine": object(), "integration_cipher": cipher, "storage": storage}, row
    )
    assert len(queries) == 1
    document = json.loads(queries[0][1]["data"])
    call_id = uuid5(
        NAMESPACE_URL, f"insurance-platform:inbound-call:{row.company_id}:provider-inbound-123"
    )
    assert document["call_log_id"] == str(call_id)
    assert document["outcome"] == "human_handoff"
    transcript = cipher.decrypt(
        base64.b64decode(document["transcript_ciphertext"]),
        associated_data=call_transcript_aad(call_id),
    ).decode()
    assert "29876543210987" not in transcript
    assert "[NATIONAL_ID]" in transcript
    assert document["recording_storage_key"] == f"call-recordings/{call_id}/recording.gcm"
    assert storage.object is not None
    assert (
        cipher.decrypt(storage.object[1], associated_data=call_recording_aad(call_id))
        == b"mock-audio-recording"
    )


async def test_worker_handles_signed_inbound_status_and_invalid_ciphertext(
    monkeypatch: pytest.MonkeyPatch, cipher: SecretCipher
) -> None:
    queries: list[tuple[str, dict[str, Any]]] = []

    async def query(_engine: Any, sql: str, params: dict[str, Any]) -> list[Any]:
        queries.append((sql, params))
        return []

    monkeypatch.setattr(worker, "_worker_query", query)
    ctx = {"engine": object(), "integration_cipher": cipher}
    status = make_job(cipher, "email_status", {"provider_message_id": "m1", "status": "delivered"})
    await _process_integration_job(ctx, status)
    assert "complete_inbound_event" in queries[-1][0]
    assert json.loads(queries[-1][1]["data"])["status"] == "delivered"

    invalid = make_job(cipher, "inbound_email", {"x": "y"})
    invalid.payload_ciphertext = b"not-an-aes-gcm-payload"
    await _process_integration_job(ctx, invalid)
    assert queries[-1][1]["state"] == "permanent"
    assert queries[-1][1]["error"] == "integration_payload_invalid"

    unsupported = make_job(cipher, "not_a_real_job", {})
    await _process_integration_job(ctx, unsupported)
    assert queries[-1][1]["state"] == "permanent"
    assert queries[-1][1]["error"] == "integration_job_unsupported"


def test_ai_result_minimization_bounds_nested_untrusted_values() -> None:
    assert _minimize_ai_value({"reply": "Contact a@example.test at +201012345678"}) == {
        "reply": "Contact [EMAIL] at [PHONE]"
    }
    assert _minimize_ai_value(["a" * 6000] * 60)[0] == "a" * 5000
    assert len(_minimize_ai_value({str(i): i for i in range(60)})) == 50
    assert _minimize_ai_value(object()).__class__ is str


async def test_imap_poll_queues_only_ticket_matched_mail_and_acknowledges_messages(
    monkeypatch: pytest.MonkeyPatch, cipher: SecretCipher
) -> None:
    company_id = uuid4()
    config = {"host": "imap.example.test", "username": "support", "password": "private"}
    config_ciphertext = cipher.encrypt_json(config, associated_data=config_aad(company_id, "imap"))
    config_row = SimpleNamespace(company_id=company_id, config_ciphertext=config_ciphertext)
    queued: list[dict[str, Any]] = []
    calls = 0

    async def query(_engine: Any, sql: str, params: dict[str, Any]) -> list[Any]:
        nonlocal calls
        calls += 1
        if "claim_imap_configs" in sql:
            return [config_row]
        queued.append(params)
        return []

    class IMAP:
        def __init__(self, settings: dict[str, Any]) -> None:
            assert settings == config
            self.marked: list[str] = []

        async def fetch_unseen(self, *, limit: int) -> list[IncomingEmail]:
            assert limit == 20
            return [
                IncomingEmail(
                    "1", "message-1", "customer@example.test", "Re: CMP-2026-000003", "Please help"
                ),
                IncomingEmail(
                    "2", "message-2", "customer@example.test", "General question", "No ticket"
                ),
            ]

        async def mark_seen(self, sequence_id: str) -> None:
            self.marked.append(sequence_id)

    mailbox = IMAP(config)
    monkeypatch.setattr(worker, "_worker_query", query)
    monkeypatch.setattr(worker, "IMAPMailProvider", lambda _config: mailbox)
    result = await poll_imap_inboxes(
        {
            "engine": object(),
            "integration_cipher": cipher,
            "integration_idempotency_key": b"i" * 32,
        }
    )
    assert result == "queued 1 inbound email message(s)"
    assert mailbox.marked == ["1", "2"]
    assert len(queued) == 1
    assert queued[0]["kind"] == "inbound_email"
    payload = cipher.decrypt_json(queued[0]["cipher"], associated_data=job_aad(queued[0]["idem"]))
    assert payload["ticket_no"] == "CMP-2026-000003"
    assert calls == 2


async def test_worker_empty_queue_reports_zero_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def query(_engine: Any, _sql: str, _params: dict[str, Any]) -> list[Any]:
        return []

    async def process(_ctx: dict[str, Any], _row: Any) -> None:
        raise AssertionError("No jobs should be processed")

    monkeypatch.setattr(worker, "_worker_query", query)
    monkeypatch.setattr(worker, "_process_integration_job", process)
    assert await process_integration_jobs({"engine": object()}) == "processed 0 integration job(s)"
