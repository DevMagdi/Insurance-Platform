"""Queries for the unified CRM module (``crm_*`` tables, ``/api/crm``).

The platform's only CRM: leads with tenant-customisable pipeline stages,
scheduled tasks/follow-ups, the Kanban board projection, salesperson
performance, dashboard statistics and the Excel import ledger.
Visibility is the CRM permission within the tenant (RLS keeps the tables
staff-only); `insurer_company_id` is lead data for filtering/reporting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.models.crm_module import (
    CrmActivity,
    CrmImportBatch,
    CrmImportError,
    CrmLead,
    CrmPipelineStage,
    CrmTask,
)
from app.models.tenancy import DEFAULT_TENANT_ID

_SORTS = {
    "created_at": CrmLead.created_at,
    "updated_at": CrmLead.updated_at,
    "full_name": CrmLead.full_name,
    "stage_code": CrmLead.stage_code,
    "priority": CrmLead.priority,
    "expected_premium": CrmLead.expected_premium,
    "next_followup_at": CrmLead.next_followup_at,
}


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def stage_dict(row: CrmPipelineStage, count: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(row.id),
        "code": row.code,
        "name_ar": row.name_ar,
        "name_en": row.name_en,
        "sort_order": row.sort_order,
        "is_won": row.is_won,
        "is_lost": row.is_lost,
        "is_converted": row.is_converted,
        "is_active": row.is_active,
    }
    if count is not None:
        data["count"] = count
    return data


def lead_dict(lead: CrmLead) -> dict[str, Any]:
    return {
        "id": str(lead.id),
        "lead_no": lead.lead_no,
        "full_name": lead.full_name,
        "company_name": lead.company_name,
        "phone": lead.phone,
        "email": lead.email,
        "source": lead.source,
        "stage_code": lead.stage_code,
        "priority": lead.priority,
        "insurance_type": lead.insurance_type,
        "insurer_company_id": str(lead.insurer_company_id) if lead.insurer_company_id else None,
        "expected_premium": str(lead.expected_premium)
        if lead.expected_premium is not None
        else None,
        "assigned_to": str(lead.assigned_to) if lead.assigned_to else None,
        "notes": lead.notes,
        "lost_reason": lead.lost_reason,
        "converted_corporate_client_id": (
            str(lead.converted_corporate_client_id) if lead.converted_corporate_client_id else None
        ),
        "converted_at": _iso(lead.converted_at),
        "closed_at": _iso(lead.closed_at),
        "next_followup_at": _iso(lead.next_followup_at),
        "import_batch_id": str(lead.import_batch_id) if lead.import_batch_id else None,
        "created_at": _iso(lead.created_at),
        "updated_at": _iso(lead.updated_at),
    }


def task_dict(row: CrmTask, lead: CrmLead | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    data: dict[str, Any] = {
        "id": str(row.id),
        "lead_id": str(row.lead_id),
        "type": row.type,
        "title": row.title,
        "description": row.description,
        "due_at": _iso(row.due_at),
        "status": row.status,
        "is_overdue": row.status == "pending" and row.due_at is not None and row.due_at < now,
        "assigned_to": str(row.assigned_to) if row.assigned_to else None,
        "completed_at": _iso(row.completed_at),
        "reminder_sent_at": _iso(row.reminder_sent_at),
        "created_at": _iso(row.created_at),
    }
    if lead is not None:
        data["lead_name"] = lead.company_name or lead.full_name
        data["lead_no"] = lead.lead_no
    return data


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
        "task_id": str(row.task_id) if row.task_id else None,
        "type": row.type,
        "description": row.description,
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": _iso(row.created_at),
    }


class CrmModuleRepository:
    tenant_id = DEFAULT_TENANT_ID

    # --- Pipeline stages ---------------------------------------------------
    async def stages(
        self, db: AsyncSession, *, include_inactive: bool = False
    ) -> list[CrmPipelineStage]:
        stmt = select(CrmPipelineStage).where(CrmPipelineStage.tenant_id == self.tenant_id)
        if not include_inactive:
            stmt = stmt.where(CrmPipelineStage.is_active.is_(True))
        stmt = stmt.order_by(CrmPipelineStage.sort_order, CrmPipelineStage.code)
        return list((await db.execute(stmt)).scalars())

    async def stage(self, db: AsyncSession, stage_id: UUID) -> CrmPipelineStage | None:
        stmt = select(CrmPipelineStage).where(
            CrmPipelineStage.id == stage_id, CrmPipelineStage.tenant_id == self.tenant_id
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def stage_by_code(self, db: AsyncSession, code: str) -> CrmPipelineStage | None:
        stmt = select(CrmPipelineStage).where(
            CrmPipelineStage.tenant_id == self.tenant_id, CrmPipelineStage.code == code
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def stage_lead_count(self, db: AsyncSession, code: str) -> int:
        stmt = (
            select(func.count())
            .select_from(CrmLead)
            .where(CrmLead.tenant_id == self.tenant_id, CrmLead.stage_code == code)
        )
        return int(await db.scalar(stmt) or 0)

    # --- Leads ---------------------------------------------------------------
    async def leads_page(
        self,
        db: AsyncSession,
        *,
        search: str | None = None,
        stage_code: str | None = None,
        source: str | None = None,
        priority: str | None = None,
        assigned_to: UUID | None = None,
        only_me: UUID | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        due_before: datetime | None = None,
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
                    CrmLead.company_name.ilike(like),
                    CrmLead.lead_no.ilike(like),
                    CrmLead.phone.ilike(like),
                    CrmLead.email.ilike(like),
                )
            )
        if stage_code:
            filters.append(CrmLead.stage_code == stage_code)
        if source:
            filters.append(CrmLead.source == source)
        if priority:
            filters.append(CrmLead.priority == priority)
        if assigned_to:
            filters.append(CrmLead.assigned_to == assigned_to)
        if only_me:
            filters.append(CrmLead.assigned_to == only_me)
        if date_from:
            filters.append(CrmLead.created_at >= date_from)
        if date_to:
            filters.append(CrmLead.created_at <= date_to)
        if due_before:
            filters.append(CrmLead.next_followup_at.is_not(None))
            filters.append(CrmLead.next_followup_at <= due_before)
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

    # --- Kanban board --------------------------------------------------------
    async def pipeline_board(
        self,
        db: AsyncSession,
        *,
        assigned_to: UUID | None = None,
        only_me: UUID | None = None,
        source: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        per_stage_limit: int = 50,
    ) -> dict[str, Any]:
        stages = await self.stages(db)
        count_stmt = (
            select(
                CrmLead.stage_code,
                func.count(),
                func.coalesce(func.sum(CrmLead.expected_premium), 0),
            )
            .where(CrmLead.tenant_id == self.tenant_id)
            .group_by(CrmLead.stage_code)
        )
        lead_stmt = (
            select(CrmLead)
            .where(CrmLead.tenant_id == self.tenant_id)
            .order_by(CrmLead.updated_at.desc())
            # Cards are capped per stage below; this bounds the scan.
            .limit(per_stage_limit * max(len(stages), 1) * 4)
        )
        for stmt_filter in (
            (CrmLead.assigned_to == assigned_to) if assigned_to else None,
            (CrmLead.assigned_to == only_me) if only_me else None,
            (CrmLead.source == source) if source else None,
            (CrmLead.created_at >= date_from) if date_from else None,
            (CrmLead.created_at <= date_to) if date_to else None,
        ):
            if stmt_filter is not None:
                count_stmt = count_stmt.where(stmt_filter)
                lead_stmt = lead_stmt.where(stmt_filter)
        counts = {str(code): (int(n), value) for code, n, value in await db.execute(count_stmt)}
        by_stage: dict[str, list[dict[str, Any]]] = {stage.code: [] for stage in stages}
        for lead in (await db.execute(lead_stmt)).scalars():
            bucket = by_stage.get(lead.stage_code)
            if bucket is not None and len(bucket) < per_stage_limit:
                bucket.append(lead_dict(lead))
        total = sum(n for n, _ in counts.values())
        won = lost = converted = 0
        open_value = 0
        stage_rows = []
        for stage in stages:
            n, value = counts.get(stage.code, (0, 0))
            if stage.is_won:
                won += n
            elif stage.is_lost:
                lost += n
            elif stage.is_converted:
                converted += n
            else:
                open_value += value or 0
            stage_rows.append(
                {**stage_dict(stage, n), "value": str(value or 0), "leads": by_stage[stage.code]}
            )
        closed = won + lost + converted
        return {
            "stages": stage_rows,
            "total": total,
            "won": won,
            "lost": lost,
            "converted": converted,
            "win_rate": round((won + converted) / closed * 100, 1) if closed else 0.0,
            "conversion_rate": round(converted / total * 100, 1) if total else 0.0,
            "open_pipeline_value": str(open_value),
        }

    # --- Tasks & follow-ups ----------------------------------------------------
    async def lead_tasks(self, db: AsyncSession, lead_id: UUID) -> list[dict[str, Any]]:
        stmt = (
            select(CrmTask)
            .where(CrmTask.lead_id == lead_id, CrmTask.tenant_id == self.tenant_id)
            .order_by(CrmTask.status, CrmTask.due_at)
        )
        return [task_dict(row) for row in (await db.execute(stmt)).scalars()]

    async def task(self, db: AsyncSession, task_id: UUID) -> CrmTask | None:
        stmt = select(CrmTask).where(CrmTask.id == task_id, CrmTask.tenant_id == self.tenant_id)
        return (await db.execute(stmt)).scalar_one_or_none()

    async def tasks(
        self,
        db: AsyncSession,
        *,
        status: str | None = None,
        type_: str | None = None,
        assigned_to: UUID | None = None,
        lead_assigned_to: UUID | None = None,
        lead_id: UUID | None = None,
        due: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        stmt = (
            select(CrmTask, CrmLead)
            .join(CrmLead, CrmLead.id == CrmTask.lead_id)
            .where(CrmTask.tenant_id == self.tenant_id)
            .order_by(CrmTask.due_at)
            .limit(limit)
        )
        now = datetime.now(UTC)
        if status:
            stmt = stmt.where(CrmTask.status == status)
        if type_:
            stmt = stmt.where(CrmTask.type == type_)
        if assigned_to:
            stmt = stmt.where(CrmTask.assigned_to == assigned_to)
        if lead_assigned_to:
            stmt = stmt.where(CrmLead.assigned_to == lead_assigned_to)
        if lead_id:
            stmt = stmt.where(CrmTask.lead_id == lead_id)
        if due == "overdue":
            stmt = stmt.where(CrmTask.status == "pending", CrmTask.due_at < now)
        elif due == "today":
            stmt = stmt.where(
                CrmTask.status == "pending",
                CrmTask.due_at >= now.replace(hour=0, minute=0, second=0, microsecond=0),
                CrmTask.due_at
                < now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
            )
        elif due == "upcoming":
            stmt = stmt.where(CrmTask.status == "pending", CrmTask.due_at >= now)
        return [task_dict(task, lead) for task, lead in await db.execute(stmt)]

    async def due_task_counts(
        self, db: AsyncSession, assigned_to: UUID | None = None
    ) -> dict[str, int]:
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        async def count(*conditions: Any) -> int:
            stmt = (
                select(func.count())
                .select_from(CrmTask)
                .where(
                    CrmTask.tenant_id == self.tenant_id,
                    CrmTask.status == "pending",
                    *((CrmTask.assigned_to == assigned_to,) if assigned_to else ()),
                    *conditions,
                )
            )
            return int(await db.scalar(stmt) or 0)

        return {
            "overdue": await count(CrmTask.due_at < now),
            "today": await count(
                CrmTask.due_at >= day_start, CrmTask.due_at < day_start + timedelta(days=1)
            ),
            "next_7_days": await count(
                CrmTask.due_at >= now, CrmTask.due_at < now + timedelta(days=7)
            ),
        }

    async def refresh_next_followup(self, db: AsyncSession, lead: CrmLead) -> None:
        """Recompute the lead's earliest open follow-up from its tasks."""
        stmt = select(func.min(CrmTask.due_at)).where(
            CrmTask.lead_id == lead.id, CrmTask.status == "pending"
        )
        lead.next_followup_at = await db.scalar(stmt)
        lead.updated_at = datetime.now(UTC)

    # --- Salesperson performance ---------------------------------------------
    async def sales_performance(
        self,
        db: AsyncSession,
        scope: AccessScope,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        source: str | None = None,
        stage_code: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = text(
            """
            SELECT s.id::text AS staff_id, s.full_name,
                   count(l.id) AS leads,
                   count(*) FILTER (WHERE st.is_won) AS won,
                   count(*) FILTER (WHERE st.is_lost) AS lost,
                   count(*) FILTER (WHERE st.is_converted OR l.converted_at IS NOT NULL)
                       AS converted,
                   COALESCE(sum(l.expected_premium)
                       FILTER (WHERE st.is_won OR st.is_converted), 0)
                       AS won_value,
                   COALESCE(sum(l.expected_premium) FILTER (
                       WHERE NOT COALESCE(st.is_won OR st.is_lost OR st.is_converted, false)), 0)
                       AS pipeline_value,
                   round(avg(EXTRACT(EPOCH FROM (l.closed_at - l.created_at)) / 86400)
                         FILTER (WHERE l.closed_at IS NOT NULL)::numeric, 1)
                       AS avg_close_days,
                   (SELECT count(*) FROM crm_tasks t
                     WHERE t.assigned_to = s.id AND t.status = 'done') AS tasks_done,
                   (SELECT count(*) FROM crm_tasks t
                     WHERE t.assigned_to = s.id AND t.status = 'pending'
                       AND t.due_at < now()) AS tasks_overdue
            FROM crm_leads l
            JOIN staff_users s ON s.id = l.assigned_to
            LEFT JOIN crm_pipeline_stages st
                   ON st.tenant_id = l.tenant_id AND st.code = l.stage_code
            WHERE l.tenant_id = :tenant
              AND (CAST(:date_from AS timestamptz) IS NULL OR l.created_at >= :date_from)
              AND (CAST(:date_to AS timestamptz) IS NULL OR l.created_at <= :date_to)
              AND (CAST(:source AS text) IS NULL OR l.source = :source)
              AND (CAST(:stage_code AS text) IS NULL OR l.stage_code = :stage_code)
              AND (CAST(:only_me AS uuid) IS NULL OR l.assigned_to = :only_me)
            GROUP BY s.id, s.full_name
            ORDER BY converted DESC, won DESC, leads DESC
            """
        )
        rows = await db.execute(
            sql,
            {
                "tenant": self.tenant_id,
                "date_from": date_from,
                "date_to": date_to,
                "source": source,
                "stage_code": stage_code,
                # Scoped staff only get their own row (pre-unification rule).
                "only_me": None if scope.all_companies else scope.actor.id,
            },
        )
        result = []
        for row in rows:
            data = dict(row._mapping)
            leads = int(data["leads"] or 0)
            converted = int(data["converted"] or 0)
            closed = int(data["won"] or 0) + int(data["lost"] or 0) + converted
            data["conversion_rate"] = round(converted / leads * 100, 1) if leads else 0.0
            data["win_rate"] = (
                round((int(data["won"] or 0) + converted) / closed * 100, 1) if closed else 0.0
            )
            data["won_value"] = str(data["won_value"])
            data["pipeline_value"] = str(data["pipeline_value"])
            data["avg_close_days"] = (
                str(data["avg_close_days"]) if data["avg_close_days"] is not None else None
            )
            result.append(data)
        return result

    # --- Excel import ledger ---------------------------------------------------
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

    # --- Dashboard ---------------------------------------------------------------
    async def stats(
        self, db: AsyncSession, scope: AccessScope, only_me: UUID | None = None
    ) -> dict[str, Any]:
        lead_scope = [CrmLead.tenant_id == self.tenant_id]
        if only_me is not None:
            lead_scope.append(CrmLead.assigned_to == only_me)
        total = int(
            await db.scalar(select(func.count()).select_from(CrmLead).where(*lead_scope)) or 0
        )
        stage_rows = await db.execute(
            select(CrmLead.stage_code, func.count()).where(*lead_scope).group_by(CrmLead.stage_code)
        )
        counts = {str(code): int(n) for code, n in stage_rows}
        stages = await self.stages(db)
        by_stage = [{**stage_dict(stage), "count": counts.get(stage.code, 0)} for stage in stages]
        source_rows = await db.execute(
            select(CrmLead.source, func.count()).where(*lead_scope).group_by(CrmLead.source)
        )
        by_source = {str(key): int(n) for key, n in source_rows}
        since = datetime.now(UTC) - timedelta(days=29)
        daily_rows = await db.execute(
            text(
                "SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day, count(*) "
                "FROM crm_leads WHERE tenant_id = :tenant AND created_at >= :since "
                "AND (CAST(:only_me AS uuid) IS NULL OR assigned_to = :only_me) "
                "GROUP BY 1 ORDER BY 1"
            ),
            {"tenant": self.tenant_id, "since": since, "only_me": only_me},
        )
        daily = {str(row[0]): int(row[1]) for row in daily_rows}
        series = []
        for offset in range(30):
            day = (since + timedelta(days=offset)).strftime("%Y-%m-%d")
            series.append({"date": day, "count": daily.get(day, 0)})
        converted = sum(counts.get(stage.code, 0) for stage in stages if stage.is_converted)
        recent_leads_stmt = (
            select(CrmLead).where(*lead_scope).order_by(CrmLead.created_at.desc()).limit(5)
        )
        recent_activities_stmt = (
            select(CrmActivity)
            .join(CrmLead, CrmLead.id == CrmActivity.lead_id)
            .where(
                CrmActivity.tenant_id == self.tenant_id,
                *([CrmLead.assigned_to == only_me] if only_me is not None else []),
            )
            .order_by(CrmActivity.created_at.desc())
            .limit(10)
        )
        return {
            "total_leads": total,
            "by_stage": by_stage,
            "by_source": by_source,
            "last_30_days": series,
            "conversion_rate": round(converted / total * 100, 1) if total else 0.0,
            "due_tasks": await self.due_task_counts(db, only_me),
            "top_performers": []
            if only_me is not None
            else (await self.sales_performance(db, scope))[:5],
            "recent_leads": [
                lead_dict(row) for row in (await db.execute(recent_leads_stmt)).scalars()
            ],
            "recent_imports": [] if only_me is not None else (await self.batches(db, limit=5)),
            "recent_activities": [
                activity_dict(row) for row in (await db.execute(recent_activities_stmt)).scalars()
            ],
        }
