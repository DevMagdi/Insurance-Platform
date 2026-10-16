"""Scheduled report orchestration: cron evaluation, rendering and delivery.

The database decides who may read which ticket; this module only decides WHEN a
report is due and which bytes are handed to the mail provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.schemas import ReportScheduleSpec, ReportScheduleView
from app.repositories.reports import ReportScheduleRepository, schedule_payload
from app.services.render import Cell, tabular_bytes

MAX_SCAN_DAYS = 400
REPORT_COLUMNS = (
    "ticket_no",
    "created_at",
    "company_id",
    "insurance_type",
    "status",
    "priority",
    "subject",
    "policy_number",
    "sla_due_at",
    "sla_breached",
    "resolved_at",
    "csat_score",
)


class ReportDelivery(Protocol):
    async def send_report(
        self,
        recipients: Sequence[str],
        subject: str,
        body: str,
        attachment_name: str,
        content_type: str,
        data: bytes,
    ) -> bool: ...


def _field(spec: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError("Empty cron field")
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) == 0:
                raise ValueError("Invalid cron step")
            step = int(raw_step)
        if part == "*":
            start, end = low, high
        elif "-" in part.lstrip("-"):
            raw_start, _, raw_end = part.partition("-")
            if not raw_start.isdigit() or not raw_end.isdigit():
                raise ValueError("Invalid cron range")
            start, end = int(raw_start), int(raw_end)
        else:
            if not part.isdigit():
                raise ValueError("Invalid cron value")
            start = end = int(part)
        if start < low or end > high or start > end:
            raise ValueError("Cron value out of range")
        values.update(range(start, end + 1, step))
    return values


class CronExpression:
    """Five-field cron (minute hour day-of-month month day-of-week).

    Day-of-week uses 0=Sunday, matching PostgreSQL's extract(dow). When both the
    day-of-month and day-of-week fields are restricted, the classic Vixie rule
    applies: the day matches if EITHER field matches.
    """

    def __init__(self, expression: str, time_zone: str) -> None:
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError("A cron expression needs exactly five fields")
        self.minutes = _field(fields[0], 0, 59)
        self.hours = _field(fields[1], 0, 23)
        self.days = _field(fields[2], 1, 31)
        self.months = _field(fields[3], 1, 12)
        raw_weekdays = _field(fields[4], 0, 7)
        self.weekdays = {value % 7 for value in raw_weekdays}
        self.day_restricted = fields[2].strip() != "*"
        self.weekday_restricted = fields[4].strip() != "*"
        self.time_zone = ZoneInfo(time_zone)

    def matches_day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        day_ok = day.day in self.days
        weekday_ok = ((day.weekday() + 1) % 7) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        if self.day_restricted:
            return day_ok
        if self.weekday_restricted:
            return weekday_ok
        return True

    def next_after(self, reference: datetime) -> datetime:
        local = reference.astimezone(self.time_zone)
        start = local.replace(second=0, microsecond=0)
        if start < local:
            start += timedelta(minutes=1)
        for offset in range(MAX_SCAN_DAYS):
            day = (start + timedelta(days=offset)).date()
            if not self.matches_day(day):
                continue
            midnight = datetime.combine(day, datetime.min.time(), tzinfo=self.time_zone)
            for hour in sorted(self.hours):
                for minute in sorted(self.minutes):
                    candidate = midnight.replace(hour=hour, minute=minute)
                    if candidate >= start:
                        return candidate.astimezone(UTC)
        raise ValueError("The cron expression has no run time within a year")


class ReportService:
    def __init__(self, repository: ReportScheduleRepository) -> None:
        self.repository = repository

    async def schedules(self, db: AsyncSession, scope: AccessScope) -> list[ReportScheduleView]:
        rows = await self.repository.schedules(db, scope)
        return [ReportScheduleView.model_validate(row) for row in rows]

    async def save(
        self, db: AsyncSession, schedule_id: UUID | None, item: ReportScheduleSpec
    ) -> UUID | None:
        CronExpression(item.cron_expression, item.time_zone)  # reject dead expressions early
        return await self.repository.save(db, schedule_id, schedule_payload(item))

    async def delete(self, db: AsyncSession, schedule_id: UUID) -> bool:
        return await self.repository.delete(db, schedule_id)

    async def queue_now(self, db: AsyncSession, schedule_id: UUID) -> bool:
        return await self.repository.queue_now(db, schedule_id)

    async def runs(
        self, db: AsyncSession, schedule_id: UUID, page: int, page_size: int
    ) -> list[dict[str, Any]]:
        return await self.repository.runs(db, schedule_id, (page - 1) * page_size, page_size)

    @staticmethod
    def next_run_at(expression: str, time_zone: str, reference: datetime) -> datetime:
        return CronExpression(expression, time_zone).next_after(reference)


def render_report(rows: Sequence[Any], fmt: str) -> bytes:
    """Render already-scoped rows; never queries and never widens authorization."""
    table: list[list[Cell]] = [
        [
            row.ticket_no,
            row.created_at,
            str(row.company_id),
            row.insurance_type,
            row.status,
            row.priority,
            row.subject,
            row.policy_number,
            row.sla_due_at,
            row.sla_breached,
            row.resolved_at,
            row.csat_score,
        ]
        for row in rows
    ]
    return tabular_bytes(fmt, "scheduled-report", REPORT_COLUMNS, table)


def report_message(name: str, fmt: str, row_count: int) -> tuple[str, str]:
    return (
        f"Scheduled complaint report: {name}",
        (
            f"Attached is the {fmt.upper()} report {name!r} with {row_count} ticket(s).\n"
            "Rows were selected with the owner's current company and type scope.\n"
            "The attachment is confidential; do not forward it outside the brokerage."
        ),
    )
