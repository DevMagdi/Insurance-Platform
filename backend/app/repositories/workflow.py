"""Scope-aware staff workspace reads and atomic SECURITY DEFINER commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.schemas import AssignmentRequest, SavedViewCreate
from app.models.operations import CannedReply, SavedFilter
from app.models.tickets import Complaint, Notification


class StaffWorkflowRepository:
    async def assign(self, db: AsyncSession, complaint: Complaint, item: AssignmentRequest) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.staff_assign(:id, :created, :agent, :team)"),
                {
                    "id": complaint.id,
                    "created": complaint.created_at,
                    "agent": item.agent_id,
                    "team": item.team_id,
                },
            )
        )

    async def escalate(self, db: AsyncSession, complaint: Complaint, reason: str) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.staff_escalate(:id, :created, :reason)"),
                {"id": complaint.id, "created": complaint.created_at, "reason": reason},
            )
        )

    async def merge(self, db: AsyncSession, source: Complaint, target: Complaint) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.staff_merge(:src, :src_created, :dst, :dst_created)"),
                {
                    "src": source.id,
                    "src_created": source.created_at,
                    "dst": target.id,
                    "dst_created": target.created_at,
                },
            )
        )

    async def candidates(
        self, db: AsyncSession, scope: AccessScope, complaint: Complaint
    ) -> list[dict[str, Any]]:
        # Same policyholder + insurer, matching policy or FTS subject, capped;
        # both the parent (service) and each candidate are independently scoped.
        stmt = (
            select(
                Complaint.id,
                Complaint.ticket_no,
                Complaint.status,
                Complaint.subject,
                Complaint.created_at,
            )
            .where(
                scope.predicate(),
                Complaint.id != complaint.id,
                Complaint.customer_id == complaint.customer_id,
                Complaint.company_id == complaint.company_id,
                Complaint.insurance_type == complaint.insurance_type,
                Complaint.created_at >= complaint.created_at - timedelta(days=180),
                Complaint.created_at <= complaint.created_at + timedelta(days=180),
                or_(
                    Complaint.policy_number == complaint.policy_number,
                    Complaint.search_vector.op("@@")(
                        func.plainto_tsquery("simple", complaint.subject)
                    ),
                ),
            )
            .order_by(Complaint.created_at.desc(), Complaint.id.desc())
            .limit(20)
        )
        return [
            {
                "id": r.id,
                "ticket_no": r.ticket_no,
                "status": r.status,
                "subject": r.subject,
                "created_at": r.created_at,
            }
            for r in await db.execute(stmt)
        ]

    async def canned_replies(
        self, db: AsyncSession, scope: AccessScope, complaint: Complaint
    ) -> list[dict[str, Any]]:
        if scope.actor.scope != "staff":
            return []
        rows = await db.execute(
            select(CannedReply.id, CannedReply.title, CannedReply.body_en, CannedReply.body_ar)
            .where(
                CannedReply.is_active.is_(True),
                or_(
                    CannedReply.company_id == complaint.company_id, CannedReply.company_id.is_(None)
                ),
            )
            .order_by(CannedReply.title)
            .limit(100)
        )
        return [dict(row._mapping) for row in rows]

    async def saved_views(self, db: AsyncSession, scope: AccessScope) -> list[SavedFilter]:
        if scope.actor.scope != "staff":
            return []
        rows = await db.scalars(
            select(SavedFilter)
            .where(SavedFilter.owner_staff_id == scope.actor.id)
            .order_by(SavedFilter.created_at.desc())
            .limit(50)
        )
        return list(rows.all())

    async def saved_view(
        self, db: AsyncSession, scope: AccessScope, view_id: UUID, *, lock: bool = False
    ) -> SavedFilter | None:
        if scope.actor.scope != "staff":
            return None
        stmt = select(SavedFilter).where(
            SavedFilter.id == view_id, SavedFilter.owner_staff_id == scope.actor.id
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await db.scalars(stmt)).one_or_none()

    async def create_view(
        self, db: AsyncSession, scope: AccessScope, item: SavedViewCreate
    ) -> SavedFilter:
        record = SavedFilter(
            owner_staff_id=scope.actor.id,
            name=item.name,
            criteria=item.criteria.model_dump(mode="json"),
        )
        db.add(record)
        await db.flush()
        return record

    async def update_view(
        self, db: AsyncSession, record: SavedFilter, item: SavedViewCreate
    ) -> SavedFilter:
        record.name = item.name
        record.criteria = item.criteria.model_dump(mode="json")
        await db.flush()
        return record

    async def delete_view(self, db: AsyncSession, scope: AccessScope, view_id: UUID) -> bool:
        if scope.actor.scope != "staff":
            return False
        return bool(
            await db.scalar(
                delete(SavedFilter)
                .where(SavedFilter.id == view_id, SavedFilter.owner_staff_id == scope.actor.id)
                .returning(SavedFilter.id)
            )
        )

    async def mark_read(self, db: AsyncSession, scope: AccessScope, notification_id: UUID) -> bool:
        if scope.actor.scope != "staff":
            return False
        return bool(
            await db.scalar(
                update(Notification)
                .where(
                    Notification.id == notification_id,
                    Notification.recipient_staff_id == scope.actor.id,
                )
                .values(read_at=func.coalesce(Notification.read_at, datetime.now(UTC)))
                .returning(Notification.id)
            )
        )
