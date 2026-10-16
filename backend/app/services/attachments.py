"""One-use signed multi-file uploads and reauthorized, clean-only downloads."""

from __future__ import annotations

import hashlib
import hmac
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from tempfile import SpooledTemporaryFile
from typing import Any
from uuid import UUID

import filetype
from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.schemas import UploadPlan
from app.models.tickets import Attachment, AuditLog, Complaint
from app.repositories.attachments import AttachmentRepository
from app.security.tokens import TokenManager
from app.services.storage import ObjectStorage

_ALLOWED = frozenset({"application/pdf", "image/jpeg", "image/png"})


class AttachmentService:
    def __init__(
        self, repository: AttachmentRepository, tokens: TokenManager, storage: ObjectStorage | None
    ) -> None:
        self.repository = repository
        self.tokens = tokens
        self.storage = storage

    def _configured(self) -> ObjectStorage:
        if self.storage is None:
            raise HTTPException(status_code=503, detail="Private object storage is not configured")
        return self.storage

    async def plan(
        self,
        db: AsyncSession,
        scope: AccessScope,
        sid: UUID,
        version: int,
        complaint: Complaint,
        plan: UploadPlan,
    ) -> list[dict[str, Any]]:
        self._configured()
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
        storage = self._configured()
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
            try:
                await storage.put(reservation.storage_key, buffer, actual_mime)  # type: ignore[arg-type]
            except Exception:
                raise HTTPException(status_code=503, detail="Storage unavailable") from None
        try:
            attachment = await self.repository.finalize(
                db, scope, reservation, mime=actual_mime, size=size, checksum=digest
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
        except Exception:
            # Best-effort cleanup for the object if database finalization fails.
            with suppress(Exception):
                await storage.delete(reservation.storage_key)
            raise
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
        self._configured()
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
        try:
            data = await self._configured().get(attachment.storage_key, attachment.size_bytes)
        except Exception:
            raise HTTPException(status_code=503, detail="Stored attachment unavailable") from None
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
