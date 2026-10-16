"""Guarded staff-console reads and writes backed by fixed security-definer APIs."""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas import (
    AssignmentRuleSpec,
    IntegrationConfigSpec,
    ReportScheduleSpec,
    TeamSpec,
)
from app.models.operations import ReportSchedule


def decode_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


class AdminRepository:
    async def access_options(self, db: AsyncSession) -> dict[str, Any] | None:
        value = await db.scalar(text("SELECT app_security.access_admin_options()"))
        return cast(dict[str, Any] | None, decode_json(value))

    async def teams(self, db: AsyncSession) -> list[dict[str, Any]] | None:
        value = await db.scalar(text("SELECT app_security.admin_teams()"))
        return cast(list[dict[str, Any]] | None, decode_json(value))

    async def save_team(
        self, db: AsyncSession, team_id: UUID | None, item: TeamSpec
    ) -> UUID | None:
        value: UUID | None = await db.scalar(
            text("SELECT app_security.save_team(:id, CAST(:data AS jsonb))"),
            {"id": team_id, "data": item.model_dump_json()},
        )
        return value

    async def assignment_options(self, db: AsyncSession) -> dict[str, Any] | None:
        value = await db.scalar(text("SELECT app_security.admin_assignment_options()"))
        return cast(dict[str, Any] | None, decode_json(value))

    async def assignment_rules(self, db: AsyncSession) -> list[dict[str, Any]] | None:
        value = await db.scalar(text("SELECT app_security.admin_assignment_rules()"))
        return cast(list[dict[str, Any]] | None, decode_json(value))

    async def save_assignment_rule(
        self, db: AsyncSession, rule_id: UUID | None, item: AssignmentRuleSpec
    ) -> UUID | None:
        value: UUID | None = await db.scalar(
            text("SELECT app_security.save_assignment_rule(:id, CAST(:data AS jsonb))"),
            {"id": rule_id, "data": item.model_dump_json()},
        )
        return value

    async def report_schedules(self, db: AsyncSession, owner_id: UUID) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(
                ReportSchedule.id,
                ReportSchedule.name,
                ReportSchedule.filters,
                ReportSchedule.cron_expression,
                ReportSchedule.time_zone,
                ReportSchedule.recipient_emails,
                ReportSchedule.next_run_at,
                ReportSchedule.is_active,
                ReportSchedule.created_at,
            )
            .where(ReportSchedule.owner_staff_id == owner_id)
            .order_by(ReportSchedule.created_at.desc(), ReportSchedule.id)
            .limit(100)
        )
        return [dict(row._mapping) for row in rows]

    async def save_report_schedule(
        self,
        db: AsyncSession,
        owner_id: UUID,
        schedule_id: UUID | None,
        item: ReportScheduleSpec,
    ) -> UUID | None:
        value: UUID | None = await db.scalar(
            text("SELECT app_security.save_report_schedule(:owner, :id, CAST(:data AS jsonb))"),
            {"owner": owner_id, "id": schedule_id, "data": item.model_dump_json()},
        )
        return value

    async def integrations(self, db: AsyncSession) -> list[dict[str, Any]] | None:
        value = await db.scalar(text("SELECT app_security.admin_integrations()"))
        return cast(list[dict[str, Any]] | None, decode_json(value))

    async def save_integration(
        self,
        db: AsyncSession,
        provider: str,
        item: IntegrationConfigSpec,
    ) -> bool:
        return bool(
            await db.scalar(
                text(
                    "SELECT app_security.save_integration("
                    ":provider, :company, CAST(:data AS jsonb))"
                ),
                {
                    "provider": provider,
                    "company": item.company_id,
                    "data": item.model_dump_json(),
                },
            )
        )
