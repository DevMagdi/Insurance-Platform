"""Queries for the isolated CRM module (``crm_*`` tables)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crm import DEFAULT_TENANT_ID
from app.models.crm_module import CrmActivity, CrmImportBatch, CrmImportError, CrmLead

_SORTS = {
    "created_at": CrmLead.created_at,
    "updated_at": CrmLead.updated_at,
    "full_name": CrmLead.full_name,
    "status": CrmLead.status,
}


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def lead_dict(lead: CrmLead) -> dict[str, Any]:
    return {
        "id": str(lead.id),
        "full_name": lead.full_name,
        "phone": lead.phone,
        "email": lead.email,
        "source": lead.source,
        "status": lead.status,
        "assigned_to": str(lead.assigned_to) if lead.assigned_to else None,
        "notes": lead.notes,
        "import_batch_id": str(lead.import_batch_id) if lead.import_batch_id else None,
        "created_at": _iso(lead.created_at),
        "updated_at": _iso(lead.updated_at),
    }


def batch_dict(batch: CrmImportBatch) -> dict[str, Any]:
    return {
        "id": str(batch.id),
        "file_name": batch.file_name,
        "uploaded_by": str(batch.uploaded_by) if batch.uploaded_by else None,
        "total_rows": batch.total_rows,
        "success_rows": batch.success_rows,
        "failed_rows": batch.failed_rows,
        "status": batch.status,
        "created_at": _iso(batch.created_at),
    }


def activity_dict(row: CrmActivity) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "lead_id": str(row.lead_id),
        "type": row.type,
        "description": row.description,
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": _iso(row.created_at),
    }


class CrmModuleRepository:
    tenant_id = DEFAULT_TENANT_ID

    async def leads_page(
        self,
        db: AsyncSession,
        *,
        search: str | None = None,
        status: str | None = None,
        source: str | None = None,
        assigned_to: UUID | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sort: str = "created_at",
        order: str = "desc",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        stmt = select(CrmLead).where(CrmLead.tenant_id == self.tenant_id)
        count = select(func.count()).select_from(CrmLead).where(CrmLead.tenant_id == self.tenant_id)
        filters = []
        if search:
            like = f"%{search}%"
            filters.append(
                or_(
                    CrmLead.full_name.ilike(like),
                    CrmLead.phone.ilike(like),
                    CrmLead.email.ilike(like),
                )
            )
        if status:
            filters.append(CrmLead.status == status)
        if source:
            filters.append(CrmLead.source == source)
        if assigned_to:
            filters.append(CrmLead.assigned_to == assigned_to)
        if date_from:
            filters.append(CrmLead.created_at >= date_from)
        if date_to:
            filters.append(CrmLead.created_at <= date_to)
        for condition in filters:
            stmt = stmt.where(condition)
            count = count.where(condition)
        column = _SORTS.get(sort, CrmLead.created_at)
        stmt = stmt.order_by(column.asc() if order == "asc" else column.desc())
        stmt = stmt.limit(page_size).offset((page - 1) * page_size)
        total = int(await db.scalar(count) or 0)
        rows = (await db.execute(stmt)).scalars()
        return {
            "items": [lead_dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size if total else 0,
        }

    async def lead(self, db: AsyncSession, lead_id: UUID) -> CrmLead | None:
        stmt = select(CrmLead).where(CrmLead.id == lead_id, CrmLead.tenant_id == self.tenant_id)
        return (await db.execute(stmt)).scalar_one_or_none()

    async def leads_by_ids(self, db: AsyncSession, ids: list[UUID]) -> list[CrmLead]:
        if not ids:
            return []
        stmt = select(CrmLead).where(CrmLead.tenant_id == self.tenant_id, CrmLead.id.in_(ids))
        return list((await db.execute(stmt)).scalars())

    async def activities(self, db: AsyncSession, lead_id: UUID) -> list[dict[str, Any]]:
        stmt = (
            select(CrmActivity)
            .where(CrmActivity.lead_id == lead_id)
            .order_by(CrmActivity.created_at.desc())
        )
        return [activity_dict(row) for row in (await db.execute(stmt)).scalars()]

    async def existing_contacts(
        self, db: AsyncSession, phones: list[str], emails: list[str]
    ) -> tuple[set[str], set[str]]:
        found_phones: set[str] = set()
        found_emails: set[str] = set()
        if phones:
            rows = await db.execute(
                select(CrmLead.phone).where(
                    CrmLead.tenant_id == self.tenant_id, CrmLead.phone.in_(phones)
                )
            )
            found_phones = {str(row) for row in rows.scalars() if row}
        if emails:
            rows = await db.execute(
                text(
                    "SELECT lower(email) FROM crm_leads "
                    "WHERE tenant_id = :tenant AND lower(email) = ANY(:emails)"
                ),
                {"tenant": self.tenant_id, "emails": [email.lower() for email in emails]},
            )
            found_emails = {str(row[0]) for row in rows}
        return found_phones, found_emails

    async def batches(self, db: AsyncSession, limit: int = 100) -> list[dict[str, Any]]:
        stmt = (
            select(CrmImportBatch)
            .where(CrmImportBatch.tenant_id == self.tenant_id)
            .order_by(CrmImportBatch.created_at.desc())
            .limit(limit)
        )
        return [batch_dict(row) for row in (await db.execute(stmt)).scalars()]

    async def batch(self, db: AsyncSession, batch_id: UUID) -> CrmImportBatch | None:
        stmt = select(CrmImportBatch).where(
            CrmImportBatch.id == batch_id, CrmImportBatch.tenant_id == self.tenant_id
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def batch_errors(self, db: AsyncSession, batch_id: UUID) -> list[dict[str, Any]]:
        stmt = (
            select(CrmImportError)
            .where(CrmImportError.batch_id == batch_id)
            .order_by(CrmImportError.row_number)
        )
        return [
            {
                "id": str(row.id),
                "row_number": row.row_number,
                "reason": row.reason,
                "raw_data": row.raw_data,
            }
            for row in (await db.execute(stmt)).scalars()
        ]

    async def stats(self, db: AsyncSession) -> dict[str, Any]:
        total = int(
            await db.scalar(
                select(func.count()).select_from(CrmLead).where(CrmLead.tenant_id == self.tenant_id)
            )
            or 0
        )

        async def grouped(column: Any) -> dict[str, int]:
            rows = await db.execute(
                select(column, func.count())
                .where(CrmLead.tenant_id == self.tenant_id)
                .group_by(column)
            )
            return {str(key): int(n) for key, n in rows}

        by_status = await grouped(CrmLead.status)
        by_source = await grouped(CrmLead.source)
        since = datetime.now(UTC) - timedelta(days=29)
        daily_rows = await db.execute(
            text(
                "SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day, count(*) "
                "FROM crm_leads WHERE tenant_id = :tenant AND created_at >= :since "
                "GROUP BY 1 ORDER BY 1"
            ),
            {"tenant": self.tenant_id, "since": since},
        )
        daily = {str(row[0]): int(row[1]) for row in daily_rows}
        series = []
        for offset in range(30):
            day = (since + timedelta(days=offset)).strftime("%Y-%m-%d")
            series.append({"date": day, "count": daily.get(day, 0)})
        recent_leads_stmt = (
            select(CrmLead)
            .where(CrmLead.tenant_id == self.tenant_id)
            .order_by(CrmLead.created_at.desc())
            .limit(5)
        )
        recent_activities_stmt = (
            select(CrmActivity).order_by(CrmActivity.created_at.desc()).limit(10)
        )
        converted = by_status.get("converted", 0)
        return {
            "total_leads": total,
            "by_status": by_status,
            "by_source": by_source,
            "last_30_days": series,
            "conversion_rate": round(converted / total * 100, 1) if total else 0.0,
            "recent_leads": [
                lead_dict(row) for row in (await db.execute(recent_leads_stmt)).scalars()
            ],
            "recent_imports": (await self.batches(db, limit=5)),
            "recent_activities": [
                activity_dict(row) for row in (await db.execute(recent_activities_stmt)).scalars()
            ],
        }
