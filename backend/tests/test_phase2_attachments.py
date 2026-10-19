"""Upload/download service tests: bytes land encrypted in PostgreSQL, never in a bucket."""

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
from app.security.blobs import ATTACHMENT, ENCRYPTION_OVERHEAD_BYTES, BlobCipher
from app.security.tokens import TokenManager
from app.services.attachments import AttachmentService
from app.settings import Settings

PDF = b"%PDF-1.4\n1 0 obj<</Type /Catalog>>\nendobj\n%%EOF\n"


@pytest.fixture
def fixture() -> tuple[
    AttachmentService,
    Mock,
    BlobCipher,
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
    blobs = BlobCipher(settings.encryption_keyring())
    service = AttachmentService(repo, tokens, blobs)
    scope = AccessScope.for_customer(Actor(uuid4(), "customer"))
    db = SimpleNamespace(
        execute=AsyncMock(), add=Mock(), flush=AsyncMock(), scalar=AsyncMock(return_value=True)
    )
    parent = SimpleNamespace(id=uuid4(), created_at=datetime.now(UTC))
    return service, repo, blobs, db, scope, parent, tokens


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
        complaint_id=parent.id,
        complaint_created_at=parent.created_at,
        original_filename="report.pdf",
        uploaded_by_customer_id=actor.id,
    )


async def test_plan_generates_short_lived_actor_bound_relative_urls(fixture: tuple) -> None:
    service, repo, _blobs, db, scope, parent, tokens = fixture
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


async def test_plan_reserves_no_object_storage_locator(fixture: tuple) -> None:
    """A reservation is pure metadata: there is no bucket key to hand out."""
    service, repo, _blobs, db, scope, parent, _tokens = fixture
    record = reservation(parent, scope.actor)
    assert not hasattr(record, "storage_key")
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
    result = await service.plan(db, scope, uuid4(), 0, parent, upload_plan)
    assert set(result[0]) == {"id", "upload_url", "upload_token", "expires_at", "size_bytes"}


async def test_upload_encrypts_content_before_the_row_is_written(fixture: tuple) -> None:
    service, repo, blobs, db, scope, parent, tokens = fixture
    record = reservation(parent, scope.actor)
    repo.get_reservation.return_value = record

    async def finalize(_db: object, _scope: object, _record: object, **kwargs: object):
        return SimpleNamespace(id=kwargs["attachment_id"])

    repo.finalize.side_effect = finalize
    sid = uuid4()
    token = tokens.issue(scope.actor, sid, 0, "upload", timedelta(minutes=5), resource_id=record.id)
    result = await service.upload(db, scope, sid, 0, record.id, token, body_request(PDF))

    assert result["scan_status"] == "pending"
    kwargs = repo.finalize.await_args.kwargs
    assert kwargs["size"] == len(PDF)
    assert kwargs["checksum"] == hashlib.sha256(PDF).hexdigest()
    stored = kwargs["content"]
    assert isinstance(stored, bytes) and PDF not in stored
    assert len(stored) == len(PDF) + ENCRYPTION_OVERHEAD_BYTES
    # Only the row owner's key can read the bytes back.
    assert blobs.decrypt(stored, purpose=ATTACHMENT, resource_id=kwargs["attachment_id"]) == PDF
    with pytest.raises(ValueError):
        blobs.decrypt(stored, purpose=ATTACHMENT, resource_id=uuid4())
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
    service, repo, _blobs, db, scope, parent, tokens = fixture
    record = reservation(parent, scope.actor)
    repo.get_reservation.return_value = record
    sid = uuid4()
    token = tokens.issue(scope.actor, sid, 0, "upload", timedelta(minutes=5), resource_id=record.id)
    with pytest.raises(HTTPException) as exc:
        await service.upload(db, scope, sid, 0, record.id, token, body_request(data, mime))
    assert exc.value.status_code == expected
    repo.finalize.assert_not_awaited()


async def test_upload_checksum_rejects_and_database_rollback_stores_nothing(
    fixture: tuple,
) -> None:
    service, repo, _blobs, db, scope, parent, tokens = fixture
    record = reservation(parent, scope.actor)
    record.expected_checksum_sha256 = "f" * 64
    repo.get_reservation.return_value = record
    sid = uuid4()
    token = tokens.issue(scope.actor, sid, 0, "upload", timedelta(minutes=5), resource_id=record.id)
    with pytest.raises(HTTPException) as exc:
        await service.upload(db, scope, sid, 0, record.id, token, body_request(PDF))
    assert exc.value.status_code == 422
    # A failing insert rolls the whole versioned transaction back: nothing persists.
    record.expected_checksum_sha256 = hashlib.sha256(PDF).hexdigest()
    repo.finalize.side_effect = RuntimeError("DB unavailable")
    with pytest.raises(RuntimeError):
        await service.upload(db, scope, sid, 0, record.id, token, body_request(PDF))


async def test_signed_download_reauthorizes_and_checks_integrity(fixture: tuple) -> None:
    service, repo, blobs, db, scope, parent, _tokens = fixture
    attachment_id = uuid4()
    record = SimpleNamespace(
        id=attachment_id,
        content=blobs.encrypt(PDF, purpose=ATTACHMENT, resource_id=attachment_id),
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
    # Tampered ciphertext and a foreign row's ciphertext both fail authentication.
    record.content = b"tampered" + bytes(record.content[8:])
    with pytest.raises(HTTPException) as exc:
        await service.download(db, scope, sid, 0, record.id, result["download_token"])
    assert exc.value.status_code == 503
    record.content = blobs.encrypt(PDF, purpose=ATTACHMENT, resource_id=uuid4())
    with pytest.raises(HTTPException) as exc:
        await service.download(db, scope, sid, 0, record.id, result["download_token"])
    assert exc.value.status_code == 503
    # Integrity is rechecked against the stored checksum after a successful decrypt.
    record.content = blobs.encrypt(b"other bytes", purpose=ATTACHMENT, resource_id=attachment_id)
    record.size_bytes = len(b"other bytes")
    with pytest.raises(HTTPException) as exc:
        await service.download(db, scope, sid, 0, record.id, result["download_token"])
    assert exc.value.status_code == 503


async def test_download_of_pre_revision_24_rows_is_gone_not_stale(fixture: tuple) -> None:
    """Rows created before database storage have no bytes; they are never served."""
    service, repo, _blobs, db, scope, parent, tokens = fixture
    record = SimpleNamespace(
        id=uuid4(),
        content=None,
        size_bytes=len(PDF),
        checksum_sha256=hashlib.sha256(PDF).hexdigest(),
        mime_type="application/pdf",
        original_filename="report.pdf",
        complaint_id=parent.id,
        complaint_created_at=parent.created_at,
    )
    repo.clean_attachment.return_value = record
    sid = uuid4()
    with pytest.raises(HTTPException) as exc:
        await service.download(db, scope, sid, 0, record.id, None)
    assert exc.value.status_code == 401  # A one-use token is still required.
    token = tokens.issue(
        scope.actor, sid, 0, "download", timedelta(minutes=2), resource_id=record.id
    )
    with pytest.raises(HTTPException) as exc:
        await service.download(db, scope, sid, 0, record.id, token)
    assert exc.value.status_code == 410


async def test_upload_without_configured_scanner_is_clean_and_downloadable(
    fixture: tuple,
) -> None:
    """No scanner configured: content inspection is the gate, never stuck pending."""
    service, repo, _blobs, db, scope, parent, tokens = fixture
    service.scanner_configured = AsyncMock(return_value=False)
    record = reservation(parent, scope.actor)
    repo.get_reservation.return_value = record

    async def finalize(_db: object, _scope: object, _record: object, **kwargs: object):
        return SimpleNamespace(id=kwargs["attachment_id"], scan_status="pending")

    repo.finalize.side_effect = finalize
    sid = uuid4()
    token = tokens.issue(scope.actor, sid, 0, "upload", timedelta(minutes=5), resource_id=record.id)
    result = await service.upload(db, scope, sid, 0, record.id, token, body_request(PDF))
    assert result["scan_status"] == "clean"
    db.scalar.assert_awaited()  # Clean transition goes through the SECURITY DEFINER.
    # The same clean gate used by authorize/download now passes for this row.
    repo.clean_attachment.return_value = SimpleNamespace(
        id=result["id"],
        content=_blobs.encrypt(PDF, purpose=ATTACHMENT, resource_id=result["id"]),
        size_bytes=len(PDF),
        checksum_sha256=hashlib.sha256(PDF).hexdigest(),
        mime_type="application/pdf",
        original_filename="report.pdf",
        complaint_id=parent.id,
        complaint_created_at=parent.created_at,
    )
    authorization = await service.authorize_download(db, scope, sid, 0, result["id"])
    content, _meta = await service.download(
        db, scope, sid, 0, result["id"], authorization["download_token"]
    )
    assert content == PDF


async def test_upload_with_configured_scanner_stays_pending(fixture: tuple) -> None:
    service, repo, _blobs, db, scope, parent, tokens = fixture
    service.scanner_configured = AsyncMock(return_value=True)
    record = reservation(parent, scope.actor)
    repo.get_reservation.return_value = record

    async def finalize(_db: object, _scope: object, _record: object, **kwargs: object):
        return SimpleNamespace(id=kwargs["attachment_id"], scan_status="pending")

    repo.finalize.side_effect = finalize
    sid = uuid4()
    token = tokens.issue(scope.actor, sid, 0, "upload", timedelta(minutes=5), resource_id=record.id)
    result = await service.upload(db, scope, sid, 0, record.id, token, body_request(PDF))
    assert result["scan_status"] == "pending"
    db.scalar.assert_not_awaited()
    repo.clean_attachment.return_value = None
    with pytest.raises(HTTPException) as exc:
        await service.authorize_download(db, scope, sid, 0, result["id"])
    assert exc.value.status_code == 404  # Infected/pending still blocked.
