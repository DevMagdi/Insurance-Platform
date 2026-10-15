"""Private upload/download service tests with a fake S3 adapter and no AV shortcut."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request

from app.db import Actor
from app.domain.access import AccessScope
from app.domain.schemas import UploadFilePlan, UploadPlan
from app.repositories.attachments import AttachmentRepository
from app.security.tokens import TokenManager
from app.services.attachments import AttachmentService
from app.settings import Settings

PDF = b"%PDF-1.4\n1 0 obj<</Type /Catalog>>\nendobj\n%%EOF\n"


class MemoryStorage:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def put(self, key: str, file: object, mime: str) -> None:
        self.data[key] = file.read()

    async def get(self, key: str, max_bytes: int) -> bytes:
        return self.data[key]

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.data.pop(key, None)


@pytest.fixture
def fixture() -> tuple[
    AttachmentService,
    Mock,
    MemoryStorage,
    SimpleNamespace,
    AccessScope,
    SimpleNamespace,
    TokenManager,
]:
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://app_runtime@localhost/test",
        APP_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"e" * 32).decode(),
        APP_BLIND_INDEX_KEY=base64.urlsafe_b64encode(b"b" * 32).decode(),
        JWT_SIGNING_KEY="s" * 64,
        ENVIRONMENT="test",
    )
    tokens = TokenManager(settings)
    repo = Mock(spec=AttachmentRepository)
    for name in ("reserve", "get_reservation", "finalize", "clean_attachment"):
        setattr(repo, name, AsyncMock())
    storage = MemoryStorage()
    service = AttachmentService(repo, tokens, storage)
    scope = AccessScope.for_customer(Actor(uuid4(), "customer"))
    db = SimpleNamespace(execute=AsyncMock(), add=Mock(), flush=AsyncMock())
    parent = SimpleNamespace(id=uuid4(), created_at=datetime.now(UTC))
    return service, repo, storage, db, scope, parent, tokens


def body_request(data: bytes, mime: str = "application/pdf") -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": data, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(
        {
            "type": "http",
            "method": "PUT",
            "headers": [
                (b"content-type", mime.encode()),
                (b"content-length", str(len(data)).encode()),
            ],
        },
        receive,
    )


def reservation(parent: SimpleNamespace, actor: Actor, data: bytes = PDF) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        status="reserved",
        expires_at=datetime.now(UTC) + timedelta(minutes=4),
        declared_mime="application/pdf",
        max_size_bytes=len(data),
        expected_checksum_sha256=hashlib.sha256(data).hexdigest(),
        storage_key=f"files/{uuid4()}",
        complaint_id=parent.id,
        complaint_created_at=parent.created_at,
        original_filename="report.pdf",
        uploaded_by_customer_id=actor.id,
    )


async def test_plan_generates_short_lived_actor_bound_relative_urls(fixture: tuple) -> None:
    service, repo, _storage, db, scope, parent, tokens = fixture
    record = reservation(parent, scope.actor)
    repo.reserve.return_value = [record]
    upload_plan = UploadPlan(
        files=[
            UploadFilePlan(
                filename="report.pdf",
                mime_type="application/pdf",
                size_bytes=len(PDF),
                checksum_sha256=hashlib.sha256(PDF).hexdigest(),
            )
        ]
    )
    sid = uuid4()
    result = await service.plan(db, scope, sid, 0, parent, upload_plan)
    assert result[0]["upload_url"].startswith("/api/customer/")
    assert "localhost" not in result[0]["upload_url"]
    claims = tokens.parse(result[0]["upload_token"], "upload")
    assert claims.resource_id == record.id and claims.session_id == sid
    assert claims.expires_at <= datetime.now(UTC) + timedelta(minutes=5)
    repo.reserve.return_value = []
    with pytest.raises(HTTPException) as exc:
        await service.plan(db, scope, sid, 0, parent, upload_plan)
    assert exc.value.status_code == 404


async def test_upload_inspects_magic_size_checksum_one_use_and_audits(fixture: tuple) -> None:
    service, repo, storage, db, scope, parent, tokens = fixture
    record = reservation(parent, scope.actor)
    repo.get_reservation.return_value = record
    uploaded = SimpleNamespace(id=uuid4())
    repo.finalize.return_value = uploaded
    sid = uuid4()
    token = tokens.issue(scope.actor, sid, 0, "upload", timedelta(minutes=5), resource_id=record.id)
    result = await service.upload(db, scope, sid, 0, record.id, token, body_request(PDF))
    assert result["scan_status"] == "pending"
    assert storage.data[record.storage_key] == PDF
    db.execute.assert_awaited_once()  # Customer upload audit (not an unaudited file write).
    with pytest.raises(HTTPException) as exc:
        await service.upload(db, scope, uuid4(), 0, record.id, token, body_request(PDF))
    assert exc.value.status_code == 403
    record.status = "uploaded"
    with pytest.raises(HTTPException) as exc:
        await service.upload(db, scope, sid, 0, record.id, token, body_request(PDF))
    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    "data,mime,expected",
    [
        (b"not a pdf", "application/pdf", 415),
        (PDF, "image/png", 415),
        (PDF + b"extra", "application/pdf", 413),
    ],
)
async def test_upload_rejects_mime_and_oversized(
    fixture: tuple,
    data: bytes,
    mime: str,
    expected: int,
) -> None:
    service, repo, storage, db, scope, parent, tokens = fixture
    record = reservation(parent, scope.actor)
    repo.get_reservation.return_value = record
    sid = uuid4()
    token = tokens.issue(scope.actor, sid, 0, "upload", timedelta(minutes=5), resource_id=record.id)
    with pytest.raises(HTTPException) as exc:
        await service.upload(db, scope, sid, 0, record.id, token, body_request(data, mime))
    assert exc.value.status_code == expected
    assert storage.data == {}


async def test_upload_checksum_rejects_and_storage_cleanup(fixture: tuple) -> None:
    service, repo, storage, db, scope, parent, tokens = fixture
    record = reservation(parent, scope.actor)
    record.expected_checksum_sha256 = "f" * 64
    repo.get_reservation.return_value = record
    sid = uuid4()
    token = tokens.issue(scope.actor, sid, 0, "upload", timedelta(minutes=5), resource_id=record.id)
    with pytest.raises(HTTPException) as exc:
        await service.upload(db, scope, sid, 0, record.id, token, body_request(PDF))
    assert exc.value.status_code == 422
    record.expected_checksum_sha256 = hashlib.sha256(PDF).hexdigest()
    repo.finalize.side_effect = RuntimeError("DB unavailable")
    with pytest.raises(RuntimeError):
        await service.upload(db, scope, sid, 0, record.id, token, body_request(PDF))
    assert record.storage_key in storage.deleted


async def test_signed_download_reauthorizes_and_checks_integrity(fixture: tuple) -> None:
    service, repo, storage, db, scope, parent, _tokens = fixture
    record = SimpleNamespace(
        id=uuid4(),
        storage_key="private/key",
        size_bytes=len(PDF),
        checksum_sha256=hashlib.sha256(PDF).hexdigest(),
        mime_type="application/pdf",
        original_filename="report.pdf",
        complaint_id=parent.id,
        complaint_created_at=parent.created_at,
    )
    sid = uuid4()
    repo.clean_attachment.return_value = None
    with pytest.raises(HTTPException) as exc:
        await service.authorize_download(db, scope, sid, 0, record.id)
    assert exc.value.status_code == 404  # Pending/infected never download.
    repo.clean_attachment.return_value = record
    result = await service.authorize_download(db, scope, sid, 0, record.id)
    assert result["download_url"].startswith("/api/customer/")
    storage.data[record.storage_key] = PDF
    content, meta = await service.download(db, scope, sid, 0, record.id, result["download_token"])
    assert content == PDF and meta == record
    db.execute.assert_awaited_once()  # Download audit.
    with pytest.raises(HTTPException) as exc:
        await service.download(db, scope, uuid4(), 0, record.id, result["download_token"])
    assert exc.value.status_code == 403
    repo.clean_attachment.return_value = None
    with pytest.raises(HTTPException) as exc:
        await service.download(db, scope, sid, 0, record.id, result["download_token"])
    assert exc.value.status_code == 404
    repo.clean_attachment.return_value = record
    storage.data[record.storage_key] = b"tampered" + PDF
    with pytest.raises(HTTPException) as exc:
        await service.download(db, scope, sid, 0, record.id, result["download_token"])
    assert exc.value.status_code == 503
