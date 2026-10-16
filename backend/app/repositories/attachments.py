"""Private upload reservations and scoped attachment metadata queries.

Attachment bytes live on the ``attachments`` row (revision 24), so reserving,
storing and reading a file never leaves the PostgreSQL transaction that already
enforces RLS and scope.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.enums import AttachmentScanStatus, MessageVisibility
from app.domain.schemas import UploadFilePlan
from app.models.core import AttachmentUpload
from app.models.tickets import Attachment, Complaint


class AttachmentRepository:
    async def reserve(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint: Complaint,
        files: list[UploadFilePlan],
    ) -> list[AttachmentUpload]:
        # Caller already resolved the parent with scope; verify again for writes.
        match = await db.scalar(
            select(Complaint.id).where(
                scope.predicate(),
                Complaint.id == complaint.id,
                Complaint.created_at == complaint.created_at,
            )
        )
        if match is None:
            return []
        records = [
            AttachmentUpload(
                id=uuid4(),
                complaint_id=complaint.id,
                complaint_created_at=complaint.created_at,
                uploaded_by_customer_id=scope.actor.id if scope.actor.scope == "customer" else None,
                uploaded_by_staff_id=scope.actor.id if scope.actor.scope == "staff" else None,
                visibility=MessageVisibility.PUBLIC,
                original_filename=item.filename,
                declared_mime=item.mime_type,
                expected_checksum_sha256=item.checksum_sha256,
                max_size_bytes=item.size_bytes,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            for item in files
        ]
        db.add_all(records)
        await db.flush()
        return records

    async def get_reservation(
        self,
        db: AsyncSession,
        scope: AccessScope,
        upload_id: UUID,
        *,
        lock: bool = False,
    ) -> AttachmentUpload | None:
        stmt = (
            select(AttachmentUpload)
            .join(
                Complaint,
                (Complaint.id == AttachmentUpload.complaint_id)
                & (Complaint.created_at == AttachmentUpload.complaint_created_at),
            )
            .where(scope.predicate(), AttachmentUpload.id == upload_id)
        )
        if lock:
            stmt = stmt.with_for_update(of=AttachmentUpload)
        return (await db.scalars(stmt)).one_or_none()

    async def finalize(
        self,
        db: AsyncSession,
        scope: AccessScope,
        reservation: AttachmentUpload,
        *,
        attachment_id: UUID,
        mime: str,
        size: int,
        checksum: str,
        content: bytes,
    ) -> Attachment:
        """Create the visible attachment row and its (already encrypted) bytes."""
        record = Attachment(
            id=attachment_id,
            complaint_id=reservation.complaint_id,
            complaint_created_at=reservation.complaint_created_at,
            uploaded_by_customer_id=scope.actor.id if scope.actor.scope == "customer" else None,
            uploaded_by_staff_id=scope.actor.id if scope.actor.scope == "staff" else None,
            visibility=MessageVisibility.PUBLIC,
            content=content,
            original_filename=reservation.original_filename,
            mime_type=mime,
            size_bytes=size,
            checksum_sha256=checksum,
            scan_status=AttachmentScanStatus.PENDING,
            upload_id=reservation.id,
        )
        db.add(record)
        reservation.status = "uploaded"
        reservation.finalized_at = datetime.now(UTC)
        await db.flush()
        return record

    async def clean_attachment(
        self,
        db: AsyncSession,
        scope: AccessScope,
        attachment_id: UUID,
    ) -> Attachment | None:
        return (
            await db.scalars(
                select(Attachment)
                .join(
                    Complaint,
                    (Complaint.id == Attachment.complaint_id)
                    & (Complaint.created_at == Attachment.complaint_created_at),
                )
                .where(
                    scope.predicate(),
                    Attachment.id == attachment_id,
                    Attachment.scan_status == AttachmentScanStatus.CLEAN,
                )
            )
        ).one_or_none()
