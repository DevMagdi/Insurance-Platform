"""Owner-scoped report schedules; execution is reauthorized by the worker."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.models.operations import ReportRun, ReportSchedule

# A claim is valid for 10 minutes, matching app_security.claim_due_reports.
CLAIM_TIMEOUT = timedelta(minutes=10)


class ReportScheduleRepository:
    async def schedules(self, db: AsyncSession, scope: AccessScope) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(
                ReportSchedule.id,
                ReportSchedule.name,
                ReportSchedule.filters,
                ReportSchedule.cron_expression,
                ReportSchedule.time_zone,
                ReportSchedule.recipient_emails,
                ReportSchedule.format,
                ReportSchedule.next_run_at,
                ReportSchedule.last_run_at,
                ReportSchedule.last_state,
                ReportSchedule.is_active,
                ReportSchedule.created_at,
            )
            .where(ReportSchedule.owner_staff_id == scope.actor.id)
            .order_by(ReportSchedule.created_at.desc(), ReportSchedule.id.desc())
            .limit(100)
        )
        return [dict(row._mapping) for row in rows]

    async def save(self, db: AsyncSession, schedule_id: UUID | None, payload: str) -> UUID | None:
        value: UUID | None = await db.scalar(
            text("SELECT app_security.save_report_schedule(:id, CAST(:data AS jsonb))"),
            {"id": schedule_id, "data": payload},
        )
        return value

    async def delete(self, db: AsyncSession, schedule_id: UUID) -> bool:
        value: bool | None = await db.scalar(
            text("SELECT app_security.delete_report_schedule(:id)"), {"id": schedule_id}
        )
        return bool(value)

    async def queue_now(self, db: AsyncSession, schedule_id: UUID) -> bool:
        value: bool | None = await db.scalar(
            text("SELECT app_security.run_report_schedule(:id)"), {"id": schedule_id}
        )
        return bool(value)

    async def runs(
        self, db: AsyncSession, schedule_id: UUID, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(
                ReportRun.id,
                ReportRun.state,
                ReportRun.row_count,
                ReportRun.error_detail,
                ReportRun.started_at,
                ReportRun.completed_at,
            )
            .where(ReportRun.schedule_id == schedule_id)
            .order_by(ReportRun.started_at.desc(), ReportRun.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [dict(row._mapping) for row in rows]


def schedule_payload(item: Any) -> str:
    """Serialize a validated spec; relative windows only, never absolute dates."""
    return json.dumps(item.model_dump(mode="json", exclude_none=True), separators=(",", ":"))
