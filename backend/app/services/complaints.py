"""Complaint workflow orchestration; all persistence goes through scoped repositories."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.enums import ComplaintStatus, MessageVisibility
from app.domain.schemas import ComplaintCreate, ComplaintFilter
from app.models.tickets import AuditLog, Complaint
from app.repositories.complaints import ComplaintRepository
from app.security.crypto import NationalIdProtector


def safe_csv_cell(value: str) -> str:
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


class ComplaintService:
    def __init__(
        self,
        repository: ComplaintRepository,
        protector: NationalIdProtector,
        digest_key: str,
    ) -> None:
        self.repository = repository
        self.protector = protector
        self.digest_key = digest_key.encode("utf-8")

    @staticmethod
    def summary(record: Complaint) -> dict[str, Any]:
        return {
            "id": record.id,
            "ticket_no": record.ticket_no,
            "status": record.status,
            "company_id": record.company_id,
            "insurance_type": record.insurance_type,
            "subject": record.subject,
            "priority": record.priority,
            "category_id": record.category_id,
            "channel": record.channel,
            "assigned_agent_id": record.assigned_agent_id,
            "sla_due_at": record.sla_due_at,
            "response_due_at": record.response_due_at,
            "sla_breached_at": record.sla_breached_at,
            "merged_into_id": record.merged_into_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    async def create(
        self,
        db: AsyncSession,
        scope: AccessScope,
        item: ComplaintCreate,
        key: str,
    ) -> tuple[Complaint, bool]:
        if scope.actor.scope != "customer":
            raise HTTPException(status_code=403, detail="Customer sign-in required")
        blind = self.protector.blind_index(item.national_id)
        if not await self.repository.identity_matches(db, item.mobile, blind):
            raise HTTPException(status_code=403, detail="Customer identity does not match sign-in")
        if not await self.repository.active_company(db, item.company_id, item.insurance_type):
            raise HTTPException(status_code=422, detail="Company or insurance type is unavailable")
        key_digest = hmac.new(
            self.digest_key, f"{scope.actor.id}:{key}".encode(), hashlib.sha256
        ).digest()
        payload_digest = hmac.new(
            self.digest_key, item.model_dump_json().encode(), hashlib.sha256
        ).digest()
        idempotency = await self.repository.reserve_idempotency(
            db, scope.actor.id, key_digest, payload_digest
        )
        if idempotency.expires_at <= datetime.now(UTC) or not hmac.compare_digest(
            idempotency.request_digest, payload_digest
        ):
            raise HTTPException(status_code=409, detail="Idempotency key expired or reused")
        if idempotency.complaint_id is not None:
            existing = await self.repository.get(db, scope, idempotency.complaint_id)
            if existing is None:
                raise HTTPException(status_code=409, detail="Existing complaint is unavailable")
            return existing, True
        policy_id = await self.repository.policy_for_customer(db, scope.actor.id, item)
        return await self.repository.create(
            db, customer_id=scope.actor.id, item=item, policy_id=policy_id, idempotency=idempotency
        ), False

    async def list(
        self, db: AsyncSession, scope: AccessScope, filters: ComplaintFilter
    ) -> dict[str, Any]:
        records = await self.repository.list(db, scope, filters)
        total = await self.repository.count(db, scope, filters)
        return {
            "items": [self.summary(record) for record in records],
            "total": total,
            "page": filters.page,
            "page_size": filters.page_size,
        }

    async def detail(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID
    ) -> dict[str, Any]:
        row = await self.repository.detail(db, scope, complaint_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        record, customer_name, last4, company_name = row
        return {
            **self.summary(record),
            "description": record.description,
            "policy_number": record.policy_number,
            "customer_name": customer_name,
            "national_id_masked": f"**********{last4}" if last4 else "**************",
            "company_name": company_name,
            "resolved_at": record.resolved_at,
            "reopened_count": record.reopened_count,
        }

    async def parent(self, db: AsyncSession, scope: AccessScope, complaint_id: UUID) -> Complaint:
        result = await self.repository.get(db, scope, complaint_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Complaint not found")
        return result

    async def reply(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        body: str,
        visibility: MessageVisibility = MessageVisibility.PUBLIC,
    ) -> dict[str, Any]:
        parent = await self.parent(db, scope, complaint_id)
        if scope.actor.scope == "customer" and parent.status == ComplaintStatus.RESOLVED:
            raise HTTPException(status_code=409, detail="Reopen the resolved complaint first")
        message = await self.repository.reply(db, scope, parent, body, visibility)
        return {
            "id": message.id,
            "created_at": message.created_at,
            "visibility": visibility,
            "body": message.body,
        }

    async def reopen(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID
    ) -> dict[str, Any]:
        parent = await self.parent(db, scope, complaint_id)
        if scope.actor.scope != "customer" or not await self.repository.reopen(db, parent):
            raise HTTPException(status_code=409, detail="Complaint cannot be reopened")
        return {"status": ComplaintStatus.REOPENED}

    async def csat(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        score: int,
        comment: str | None,
    ) -> dict[str, Any]:
        parent = await self.parent(db, scope, complaint_id)
        if scope.actor.scope != "customer" or not await self.repository.csat(
            db, parent, score, comment
        ):
            raise HTTPException(status_code=409, detail="Feedback is unavailable")
        return {"score": score, "submitted": True}

    async def change_status(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        status: ComplaintStatus,
    ) -> dict[str, Any]:
        parent = await self.parent(db, scope, complaint_id)
        if scope.actor.scope != "staff" or not await self.repository.change_status(
            db, parent, status
        ):
            raise HTTPException(status_code=409, detail="Invalid status transition")
        return {"status": status}

    async def reveal_national_id(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        reason: str,
    ) -> str:
        if scope.actor.scope != "staff" or len(reason.strip()) < 10:
            raise HTTPException(
                status_code=400, detail="A reason of at least 10 characters is required"
            )
        parent = await self.parent(db, scope, complaint_id)
        ciphertext = await self.repository.reveal_national_id(db, parent.customer_id)
        if ciphertext is None:
            raise HTTPException(status_code=403, detail="National ID reveal not permitted")
        value = self.protector.decrypt(ciphertext)
        await self.repository.audit_reveal(
            db, staff_id=scope.actor.id, complaint=parent, reason=reason.strip()[:200]
        )
        return value

    async def export(
        self,
        db: AsyncSession,
        scope: AccessScope,
        filters: ComplaintFilter,
    ) -> bytes:
        if scope.actor.scope != "staff":
            raise HTTPException(status_code=403, detail="Staff access required")
        records = await self.repository.list(db, scope, filters, export_limit=1001)
        if len(records) > 1000:
            raise HTTPException(
                status_code=413, detail="Narrow filters to 1000 complaints or fewer"
            )
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "ticket_no",
                "created_at",
                "company_id",
                "insurance_type",
                "status",
                "subject",
                "policy_number",
            ]
        )
        for row in records:
            # Spreadsheet formula injection must not turn user text into executable cells.
            writer.writerow(
                [
                    row.ticket_no,
                    row.created_at.isoformat(),
                    str(row.company_id),
                    row.insurance_type.value,
                    row.status.value,
                    safe_csv_cell(row.subject),
                    safe_csv_cell(row.policy_number),
                ]
            )
        db.add(
            AuditLog(
                actor_staff_id=scope.actor.id,
                action="complaints_export",
                resource_type="complaints",
                after_state={"count": len(records)},
            )
        )
        await db.flush()
        return output.getvalue().encode("utf-8-sig")
