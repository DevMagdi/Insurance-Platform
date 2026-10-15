"""ARQ outbox, fail-closed scanner, restricted DB and stub delivery adapters."""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.providers import ProviderUnavailable, SMTPDelivery, UnconfiguredVirusScanner
from app.services.storage import S3ObjectStorage
from app.settings import Settings
from app.worker import _worker_query, deliver_notices, health_job, scan_pending


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
        S3_ENDPOINT_URL="http://minio:9000",
        S3_ACCESS_KEY_ID="dev",
        S3_SECRET_ACCESS_KEY="dev-secret",
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


async def test_scanner_stub_cannot_mark_clean() -> None:
    assert await UnconfiguredVirusScanner().scan("private/key") is None


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


async def test_worker_scan_only_records_explicit_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    upload = uuid4()
    calls: list[tuple[str, dict]] = []

    async def query(engine: object, sql: str, params: dict):
        calls.append((sql, params))
        if "pending_scans" in sql:
            return [SimpleNamespace(upload_id=upload, storage_key="private/file")]
        return []

    monkeypatch.setattr("app.worker._worker_query", query)
    scanner = SimpleNamespace(scan=AsyncMock(return_value=None))
    ctx = {"engine": SimpleNamespace(), "scanner": scanner}
    assert await scan_pending(ctx) == "checked 1 pending scan(s)"
    assert len(calls) == 1
    scanner.scan.return_value = "clean"
    await scan_pending(ctx)
    assert calls[-1][1] == {"id": upload, "verdict": "clean"}
    assert await health_job({}) == "ok"


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


async def test_private_s3_adapter_limits_reads_and_deletes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    values: dict[str, bytes] = {}

    class FakeS3:
        def upload_fileobj(self, stream: BytesIO, bucket: str, key: str, ExtraArgs: dict):
            assert (
                bucket == "complaint-attachments" and ExtraArgs["ContentType"] == "application/pdf"
            )
            values[key] = stream.read()

        def get_object(self, Bucket: str, Key: str):
            return {"Body": BytesIO(values[Key])}

        def delete_object(self, Bucket: str, Key: str):
            values.pop(Key, None)

    monkeypatch.setattr("app.services.storage.boto3.client", lambda *args, **kw: FakeS3())
    storage = S3ObjectStorage(settings)
    await storage.put("private/key", BytesIO(b"%PDF"), "application/pdf")
    assert await storage.get("private/key", 4) == b"%PDF"
    with pytest.raises(ValueError):
        await storage.get("private/key", 3)
    await storage.delete("private/key")
    assert values == {}


async def test_worker_startup_uses_isolated_role_and_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_DATABASE_URL", "postgresql+asyncpg://app_worker@localhost/test")
    fake_engine = SimpleNamespace(dispose=AsyncMock())
    from unittest.mock import Mock

    factory = Mock(return_value=fake_engine)
    monkeypatch.setattr("app.worker.make_worker_engine", factory)
    from app.worker import shutdown, startup

    context: dict = {}
    await startup(context)
    assert context["engine"] is fake_engine
    assert factory.call_args.args[0].startswith("postgresql+asyncpg://app_worker")
    assert await context["scanner"].scan("private/unscanned") is None
    await shutdown(context)
    fake_engine.dispose.assert_awaited_once()
