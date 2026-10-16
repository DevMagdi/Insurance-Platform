"""ARQ outbox, fail-closed scanner, restricted DB and stub delivery adapters."""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.security.blobs import ATTACHMENT, EXPORT, BlobCipher
from app.services.providers import (
    ProviderUnavailable,
    ScanTarget,
    SMTPDelivery,
    UnconfiguredVirusScanner,
)
from app.settings import Settings
from app.worker import (
    _worker_query,
    clean_expired_exports,
    deliver_notices,
    export_pending,
    health_job,
    scan_pending,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="development",
        SMTP_HOST="localhost",
        DEV_SMS_MAILBOX=True,
    )


async def test_mail_adapter_dev_otp_invite_and_notices(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = AsyncMock()
    monkeypatch.setattr("app.services.providers.aiosmtplib.send", sent)
    provider = SMTPDelivery(settings)
    await provider.send_otp("+201123456789", "123456")
    message = sent.call_args.args[0]
    assert "sms-201123456789@sms.local.test" in message["To"]
    assert "123456" in message.get_content()
    await provider.send_invitation("staff@example.test", "https://example.test/invite?token=secret")
    assert sent.call_args.args[0]["To"] == "staff@example.test"
    assert await provider.send_notice(
        "+201123456789",
        None,
        "status_changed",
        {"ticket_no": "CMP-2026-000001", "status": "resolved"},
    )
    settings.dev_sms_mailbox = False
    with pytest.raises(ProviderUnavailable):
        await provider.send_otp("+201123456789", "123456")
    assert not await provider.send_notice(
        "+201123456789", None, "created", {"ticket_no": "CMP-2026-000001", "status": "new"}
    )
    settings.smtp_host = None
    with pytest.raises(ProviderUnavailable):
        await provider.send_invitation("staff@example.test", "https://example.test/")
    assert not await provider.send_notice(
        "+201123456789",
        "customer@example.test",
        "created",
        {"ticket_no": "CMP-2026-000001", "status": "new"},
    )


def scan_target(blobs: BlobCipher, content: bytes = b"%PDF-1.4 scan me") -> ScanTarget:
    attachment_id = uuid4()
    return ScanTarget(
        attachment_id=attachment_id,
        original_filename="report.pdf",
        mime_type="application/pdf",
        size_bytes=len(content),
        checksum_sha256="a" * 64,
        content=blobs.decrypt(
            blobs.encrypt(content, purpose=ATTACHMENT, resource_id=attachment_id),
            purpose=ATTACHMENT,
            resource_id=attachment_id,
        ),
    )


async def test_scanner_stub_cannot_mark_clean() -> None:
    assert await UnconfiguredVirusScanner().scan(scan_target(BlobCipher(b"k" * 32))) is None


async def test_worker_claims_notices_and_acks_or_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    notice = uuid4()
    calls: list[tuple[str, dict]] = []

    async def query(engine: object, sql: str, params: dict):
        calls.append((sql, params))
        if "claim_notices" in sql:
            return [
                SimpleNamespace(
                    notice_id=notice,
                    mobile="+201123456789",
                    email=None,
                    kind="created",
                    payload={"ticket_no": "CMP-2026-000001", "status": "new"},
                )
            ]
        return []

    monkeypatch.setattr("app.worker._worker_query", query)
    delivery = SimpleNamespace(send_notice=AsyncMock(return_value=True))
    ctx = {"engine": SimpleNamespace(), "delivery": delivery}
    assert await deliver_notices(ctx) == "processed 1 notice(s)"
    assert calls[-1][1] == {"id": notice, "state": "sent"}
    delivery.send_notice.return_value = False
    await deliver_notices(ctx)
    assert calls[-1][1]["state"] == "pending_provider"
    delivery.send_notice.side_effect = RuntimeError("Transient SMTP failure")
    await deliver_notices(ctx)
    assert calls[-1][1]["state"] == "retry"


async def test_worker_scan_decrypts_one_row_and_only_records_explicit_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = uuid4()
    attachment = uuid4()
    content = b"%PDF-1.4 real content"
    blobs = BlobCipher(b"k" * 32)
    stored = blobs.encrypt(content, purpose=ATTACHMENT, resource_id=attachment)
    calls: list[tuple[str, dict]] = []

    async def query(engine: object, sql: str, params: dict):
        calls.append((sql, params))
        if "pending_scans" in sql:
            return [
                SimpleNamespace(
                    upload_id=upload,
                    attachment_id=attachment,
                    original_filename="report.pdf",
                    mime_type="application/pdf",
                    size_bytes=len(content),
                    checksum_sha256="a" * 64,
                )
            ]
        if "attachment_scan_content" in sql:
            return [SimpleNamespace(content=stored)]
        return []

    monkeypatch.setattr("app.worker._worker_query", query)
    scanner = SimpleNamespace(scan=AsyncMock(return_value=None))
    ctx = {"engine": SimpleNamespace(), "scanner": scanner, "blobs": blobs}
    assert await scan_pending(ctx) == "checked 1 pending scan(s)"
    scanner.scan.assert_awaited_once()
    target = scanner.scan.await_args.args[0]
    assert target.content == content and target.attachment_id == attachment
    # An unconfigured scanner records nothing; only an explicit verdict is stored.
    assert len(calls) == 2
    scanner.scan.return_value = "clean"
    await scan_pending(ctx)
    assert calls[-1][1] == {"id": upload, "verdict": "clean"}
    assert await health_job({}) == "ok"


async def test_worker_scan_marks_unreadable_content_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row that cannot be decrypted (or is missing) fails closed, never silently pending."""
    calls: list[tuple[str, dict]] = []

    async def query(engine: object, sql: str, params: dict):
        calls.append((sql, params))
        if "pending_scans" in sql:
            return [
                SimpleNamespace(
                    upload_id=uuid4(),
                    attachment_id=uuid4(),
                    original_filename="report.pdf",
                    mime_type="application/pdf",
                    size_bytes=4,
                    checksum_sha256="b" * 64,
                )
            ]
        if "attachment_scan_content" in sql:
            return [SimpleNamespace(content=None)]
        return []

    monkeypatch.setattr("app.worker._worker_query", query)
    scanner = SimpleNamespace(scan=AsyncMock(return_value="clean"))
    ctx = {"engine": SimpleNamespace(), "scanner": scanner, "blobs": BlobCipher(b"k" * 32)}
    await scan_pending(ctx)
    scanner.scan.assert_not_awaited()
    assert calls[-1][1]["verdict"] == "error"


async def test_worker_db_context_only_sets_worker_scope() -> None:
    executed: list[str] = []

    class Conn:
        async def execute(self, statement: object, params: dict | None = None):
            executed.append(str(statement))
            return SimpleNamespace(all=lambda: [(1,)])

    @asynccontextmanager
    async def begin():
        yield Conn()

    engine = SimpleNamespace(begin=begin)
    assert await _worker_query(
        engine, "SELECT app_security.pending_scans(:limit)", {"limit": 2}
    ) == [(1,)]
    assert "set_config('app.scope', 'worker', true)" in executed[0]
    assert "pending_scans" in executed[1]


async def test_export_jobs_store_the_artifact_encrypted_on_the_job_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    blobs = BlobCipher(b"k" * 32)
    calls: list[tuple[str, dict]] = []

    async def query(engine: object, sql: str, params: dict):
        calls.append((sql, params))
        if "claim_exports" in sql:
            return [SimpleNamespace(job_id=job_id, attempt=1, kind="complaints", format="csv")]
        if "export_rows" in sql:
            return [
                SimpleNamespace(
                    complaint_id=uuid4(),
                    complaint_created_at=datetime.now(UTC),
                    ticket_no="CMP-2026-000001",
                    created_at=datetime.now(UTC),
                    company_id=uuid4(),
                    insurance_type="health",
                    status="new",
                    subject="Late claim",
                    policy_number="POL-1",
                    sla_breached=False,
                    resolved_at=None,
                    csat_score=None,
                )
            ]
        return [SimpleNamespace(finished=True)]

    monkeypatch.setattr("app.worker._worker_query", query)
    ctx = {"engine": SimpleNamespace(), "blobs": blobs}
    assert await export_pending(ctx) == "processed 1 export job(s)"
    stored = next(
        params for sql, params in calls if "finish_export" in sql and params.get("content")
    )
    assert (
        "CMP-2026-000001"
        in blobs.decrypt(stored["content"], purpose=EXPORT, resource_id=job_id).decode()
    )
    assert "key" not in stored


async def test_expired_export_cleanup_nullifies_the_stored_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    calls: list[tuple[str, dict]] = []

    async def query(engine: object, sql: str, params: dict):
        calls.append((sql, params))
        if "claim_export_cleanup" in sql:
            return [SimpleNamespace(job_id=job_id, has_content=True)]
        return [SimpleNamespace(finished=True)]

    monkeypatch.setattr("app.worker._worker_query", query)
    assert await clean_expired_exports({"engine": SimpleNamespace()}) == (
        "cleaned 1 expired export artifact(s)"
    )
    assert calls[-1][1] == {"id": job_id}


async def test_worker_startup_uses_isolated_role_and_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_DATABASE_URL", "postgresql+asyncpg://app_worker@localhost/test")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    fake_engine = SimpleNamespace(dispose=AsyncMock())
    from unittest.mock import Mock

    factory = Mock(return_value=fake_engine)
    monkeypatch.setattr("app.worker.make_worker_engine", factory)
    from app.worker import shutdown, startup

    context: dict = {}
    await startup(context)
    assert context["engine"] is fake_engine
    assert factory.call_args.args[0].startswith("postgresql+asyncpg://app_worker")
    assert await context["scanner"].scan(scan_target(context["blobs"])) is None
    # The worker holds the deployment keyring, never an object-storage client.
    assert isinstance(context["blobs"], BlobCipher)
    assert "storage" not in context
    await shutdown(context)
    fake_engine.dispose.assert_awaited_once()
