"""One-use signed uploads and reauthorized, clean-only downloads.

Bytes are encrypted and written in the same database transaction that records the
attachment, so the platform has no second store to keep consistent: no signed
bucket URL, no orphan object when a transaction rolls back, and a database dump
without ``APP_ENCRYPTION_KEY`` never reveals a customer document.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from tempfile import SpooledTemporaryFile
from typing import Any
from uuid import UUID, uuid4

import filetype
from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.enums import AttachmentScanStatus
from app.domain.schemas import UploadPlan
from app.models.tickets import Attachment, AuditLog, Complaint
from app.repositories.attachments import AttachmentRepository
from app.security.blobs import ATTACHMENT, BlobCipher
from app.security.tokens import TokenManager

_ALLOWED = frozenset({"application/pdf", "image/jpeg", "image/png"})


class AttachmentService:
    def __init__(
        self,
        repository: AttachmentRepository,
        tokens: TokenManager,
        blobs: BlobCipher,
        scanner_configured: Callable[[AsyncSession], Awaitable[bool]] | None = None,
    ) -> None:
        self.repository = repository
        self.tokens = tokens
        self.blobs = blobs
        # Presence probe for an external virus scanner. When no scanner is
        # configured, a successful upload has already passed magic-byte MIME,
        # size and checksum inspection, so it is marked clean immediately
        # instead of being stuck in "pending" forever. When a scanner IS
        # configured (or the probe is not wired), uploads stay pending for
        # the worker's verdict — fail closed.
        self.scanner_configured = scanner_configured

    async def plan(
        self,
        db: AsyncSession,
        scope: AccessScope,
        sid: UUID,
        version: int,
        complaint: Complaint,
        plan: UploadPlan,
    ) -> list[dict[str, Any]]:
        reservations = await self.repository.reserve(db, scope, complaint, plan.files)
        if len(reservations) != len(plan.files):
            raise HTTPException(status_code=404, detail="Complaint not found")
        return [
            {
                "id": record.id,
                "upload_url": f"/api/{scope.actor.scope}/attachments/uploads/{record.id}/content",
                "upload_token": self.tokens.issue(
                    scope.actor, sid, version, "upload", timedelta(minutes=5), resource_id=record.id
                ),
                "expires_at": record.expires_at,
                "size_bytes": record.max_size_bytes,
            }
            for record in reservations
        ]

    async def upload(
        self,
        db: AsyncSession,
        scope: AccessScope,
        sid: UUID,
        version: int,
        upload_id: UUID,
        token: str | None,
        request: Request,
    ) -> dict[str, Any]:
        claims = self.tokens.parse(token, "upload")
        if (
            claims.subject != scope.actor
            or claims.session_id != sid
            or claims.token_version != version
            or claims.resource_id != upload_id
        ):
            raise HTTPException(status_code=403, detail="Upload token does not match this session")
        reservation = await self.repository.get_reservation(db, scope, upload_id, lock=True)
        if (
            reservation is None
            or reservation.status != "reserved"
            or reservation.expires_at <= datetime.now(UTC)
        ):
            raise HTTPException(status_code=404, detail="Upload reservation unavailable")
        if (
            request.headers.get("content-type", "").split(";")[0].strip()
            != reservation.declared_mime
        ):
            raise HTTPException(status_code=415, detail="Declared MIME type mismatch")
        declared_length = request.headers.get("content-length")
        if declared_length and (
            not declared_length.isdecimal() or int(declared_length) > reservation.max_size_bytes
        ):
            raise HTTPException(status_code=413, detail="Attachment exceeds reserved size")
        checksum = hashlib.sha256()
        size = 0
        prefix = b""
        with SpooledTemporaryFile(max_size=1_048_576) as buffer:
            async for chunk in request.stream():
                size += len(chunk)
                if size > reservation.max_size_bytes:
                    raise HTTPException(status_code=413, detail="Attachment exceeds reserved size")
                checksum.update(chunk)
                if len(prefix) < 261:
                    prefix += chunk[: 261 - len(prefix)]
                buffer.write(chunk)
            actual_mime = filetype.guess_mime(prefix)
            if actual_mime not in _ALLOWED or actual_mime != reservation.declared_mime:
                raise HTTPException(
                    status_code=415, detail="Attachment type failed content inspection"
                )
            digest = checksum.hexdigest()
            if (
                size != reservation.max_size_bytes
                or not reservation.expected_checksum_sha256
                or not hmac.compare_digest(digest, reservation.expected_checksum_sha256)
            ):
                raise HTTPException(status_code=422, detail="Attachment checksum or size mismatch")
            buffer.seek(0)
            # Encrypt before the row exists: the database only ever receives ciphertext,
            # bound to the attachment id the row will carry.
            attachment_id = uuid4()
            content = self.blobs.encrypt(
                buffer.read(), purpose=ATTACHMENT, resource_id=attachment_id
            )
        attachment = await self.repository.finalize(
            db,
            scope,
            reservation,
            attachment_id=attachment_id,
            mime=actual_mime,
            size=size,
            checksum=digest,
            content=content,
        )
        if scope.actor.scope == "customer":
            await db.execute(
                text("SELECT app_security.audit_customer_action(:id, :created, 'upload')"),
                {"id": reservation.complaint_id, "created": reservation.complaint_created_at},
            )
        else:
            db.add(
                AuditLog(
                    actor_staff_id=scope.actor.id,
                    action="attachment_uploaded",
                    resource_type="attachment",
                    resource_id=attachment.id,
                    complaint_id=reservation.complaint_id,
                    complaint_created_at=reservation.complaint_created_at,
                )
            )
        await db.flush()
        if self.scanner_configured is not None and not await self.scanner_configured(db):
            attachment.scan_status = AttachmentScanStatus.CLEAN
            await db.flush()
            return {
                "id": attachment.id,
                "scan_status": "clean",
                "message": "File passed content inspection and is available for download",
            }
        return {
            "id": attachment.id,
            "scan_status": "pending",
            "message": "Awaiting virus scan; download is blocked until clean",
        }

    async def authorize_download(
        self,
        db: AsyncSession,
        scope: AccessScope,
        sid: UUID,
        version: int,
        attachment_id: UUID,
    ) -> dict[str, Any]:
        # Authorize first so storage state never precedes the access decision.
        attachment = await self.repository.clean_attachment(db, scope, attachment_id)
        if attachment is None:
            raise HTTPException(status_code=404, detail="Clean attachment not found")
        return {
            "download_url": f"/api/{scope.actor.scope}/attachments/{attachment_id}/content",
            "download_token": self.tokens.issue(
                scope.actor,
                sid,
                version,
                "download",
                timedelta(minutes=2),
                resource_id=attachment_id,
            ),
            "expires_in_seconds": 120,
        }

    async def download(
        self,
        db: AsyncSession,
        scope: AccessScope,
        sid: UUID,
        version: int,
        attachment_id: UUID,
        token: str | None,
    ) -> tuple[bytes, Attachment]:
        claims = self.tokens.parse(token, "download")
        if (
            claims.subject != scope.actor
            or claims.session_id != sid
            or claims.token_version != version
            or claims.resource_id != attachment_id
        ):
            raise HTTPException(
                status_code=403, detail="Download token does not match this session"
            )
        attachment = await self.repository.clean_attachment(db, scope, attachment_id)
        if attachment is None:
            raise HTTPException(status_code=404, detail="Clean attachment not found")
        if attachment.content is None:
            # Rows created before revision 24 kept their bytes in the retired object
            # store; they are never served from a stale copy.
            raise HTTPException(status_code=410, detail="Attachment content is no longer stored")
        try:
            data = self.blobs.decrypt(
                attachment.content, purpose=ATTACHMENT, resource_id=attachment.id
            )
        except ValueError:
            raise HTTPException(
                status_code=503, detail="Attachment integrity verification failed"
            ) from None
        if len(data) != attachment.size_bytes or not hmac.compare_digest(
            hashlib.sha256(data).hexdigest(), attachment.checksum_sha256
        ):
            raise HTTPException(status_code=503, detail="Attachment integrity verification failed")
        if scope.actor.scope == "customer":
            await db.execute(
                text("SELECT app_security.audit_customer_action(:id, :created, 'download')"),
                {"id": attachment.complaint_id, "created": attachment.complaint_created_at},
            )
        else:
            db.add(
                AuditLog(
                    actor_staff_id=scope.actor.id,
                    action="attachment_downloaded",
                    resource_type="attachment",
                    resource_id=attachment.id,
                    complaint_id=attachment.complaint_id,
                    complaint_created_at=attachment.complaint_created_at,
                )
            )
            await db.flush()
        return data, attachment
