"""Additional mocked worker dispatch and IMAP-ingest coverage for Phase 4."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from app import worker
from app.security.blobs import RECORDING, BlobCipher
from app.security.integration_crypto import SecretCipher
from app.services.integration_providers import (
    IncomingEmail,
    ProviderResult,
    WhatsAppSendRequest,
)
from app.services.integrations import call_transcript_aad, config_aad, job_aad
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

    blobs = BlobCipher(b"b" * 32)
    row = make_job(
        cipher,
        "inbound_voice",
        {
            "provider_call_id": "provider-inbound-123",
            "ticket_no": "CMP-2026-000001",
            "consent_confirmed": True,
            "request_human": True,
            "text": "Customer 29876543210987 asked for a human agent.",
            "recording_b64": base64.b64encode(b"mock-audio-recording").decode("ascii"),
        },
        provider="twilio",
    )
    monkeypatch.setattr(worker, "_worker_query", query)
    await _process_integration_job(
        {"engine": object(), "integration_cipher": cipher, "blobs": blobs}, row
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
    # The recording travels as ciphertext bound to the call id: no bucket key exists.
    assert "recording_storage_key" not in document
    stored = base64.b64decode(document["recording_ciphertext"])
    assert blobs.decrypt(stored, purpose=RECORDING, resource_id=call_id) == b"mock-audio-recording"
    with pytest.raises(ValueError):
        blobs.decrypt(stored, purpose=RECORDING, resource_id=uuid4())


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


async def test_imap_poll_queues_mail_with_or_without_a_ticket_and_skips_only_junk(
    monkeypatch: pytest.MonkeyPatch, cipher: SecretCipher
) -> None:
    """A customer who writes in without quoting a ticket still gets a request.

    This used to drop every ticket-less mail on the floor: no ticket meant
    mark-as-seen and discard, so the message was gone from the mailbox and
    absent from the platform. Only mail with no usable sender, or no content
    at all, may be skipped now.
    """
    company_id = uuid4()
    config = {"host": "imap.example.test", "username": "support", "password": "private"}
    config_ciphertext = cipher.encrypt_json(config, associated_data=config_aad(company_id, "imap"))
    config_row = SimpleNamespace(
        config_id=uuid4(),
        company_id=company_id,
        provider="imap",
        config_ciphertext=config_ciphertext,
    )
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
                    "1", "message-1", "customer@example.test", "Re: REQ-2026-000003", "Please help"
                ),
                IncomingEmail(
                    "2",
                    "message-2",
                    '"سعاد فؤاد" <soad@example.test>',
                    "General question",
                    "No ticket",
                ),
                # No sender to reply to and nothing to act on: this one is junk.
                IncomingEmail("3", "message-3", "", "", ""),
            ]

        async def mark_seen(self, sequence_id: str) -> None:
            self.marked.append(sequence_id)

    mailbox = IMAP(config)
    monkeypatch.setattr(worker, "_worker_query", query)
    monkeypatch.setattr(worker, "INBOUND_EMAIL_PROVIDER_CLASSES", {"imap": lambda _config: mailbox})
    result = await poll_imap_inboxes(
        {
            "engine": object(),
            "integration_cipher": cipher,
            "integration_idempotency_key": b"i" * 32,
        }
    )
    assert result == "queued 2 inbound email message(s)"
    # Every message is acknowledged so the mailbox does not re-deliver it.
    assert mailbox.marked == ["1", "2", "3"]
    assert len(queued) == 2
    assert {row["kind"] for row in queued} == {"inbound_email"}
    assert {row["provider"] for row in queued} == {"imap"}

    first = cipher.decrypt_json(queued[0]["cipher"], associated_data=job_aad(queued[0]["idem"]))
    assert first["ticket_no"] == "REQ-2026-000003"

    second = cipher.decrypt_json(queued[1]["cipher"], associated_data=job_aad(queued[1]["idem"]))
    assert second["ticket_no"] is None
    assert second["sender"] == "soad@example.test"
    # The display name is carried through so the new request is not anonymous.
    assert second["sender_name"] == "سعاد فؤاد"
    assert second["subject"] == "General question"
    assert calls == 3


async def test_worker_empty_queue_reports_zero_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def query(_engine: Any, _sql: str, _params: dict[str, Any]) -> list[Any]:
        return []

    async def process(_ctx: dict[str, Any], _row: Any) -> None:
        raise AssertionError("No jobs should be processed")

    monkeypatch.setattr(worker, "_worker_query", query)
    monkeypatch.setattr(worker, "_process_integration_job", process)
    assert await process_integration_jobs({"engine": object()}) == "processed 0 integration job(s)"
