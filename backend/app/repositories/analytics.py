"""Aggregates are built from the SAME AccessScope + RLS-filtered complaint set."""

from __future__ import annotations

from datetime import time
from typing import Any

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.enums import ComplaintStatus
from app.domain.schemas import ComplaintFilter
from app.models.access import StaffUser
from app.models.identity import InsuranceCompany
from app.models.tickets import Category, Complaint
from app.repositories.complaints import ComplaintRepository


def whole_utc_days(filters: ComplaintFilter) -> bool:
    """The day-granular rollup answers whole UTC days only (midnight to midnight)."""
    if filters.date_from is None or filters.date_to is None:
        return False
    if filters.search is not None or filters.ticket_no is not None:
        return False
    midnight = time(0, 0)
    if filters.date_from.timetz().replace(tzinfo=None) != midnight:
        return False
    if filters.date_to.timetz().replace(tzinfo=None) != midnight:
        return False
    return filters.date_from < filters.date_to


class AnalyticsRepository:
    async def report(
        self, db: AsyncSession, scope: AccessScope, filters: ComplaintFilter
    ) -> dict[str, Any]:
        # Only safe aggregate columns leave this query; customer names/IDs do not.
        source = (
            ComplaintRepository()
            .base_query(scope, filters)
            .with_only_columns(
                Complaint.id,
                Complaint.created_at,
                Complaint.resolved_at,
                Complaint.status,
                Complaint.company_id,
                Complaint.insurance_type,
                Complaint.assigned_agent_id,
                Complaint.category_id,
                Complaint.sla_breached_at,
                Complaint.reopened_count,
                Complaint.csat_score,
                maintain_column_froms=True,
            )
            .subquery("visible_tickets")
        )
        hours = func.extract("epoch", source.c.resolved_at - source.c.created_at) / 3600
        rows = await db.execute(
            select(
                func.count().label("total"),
                func.avg(hours).filter(source.c.resolved_at.is_not(None)).label("avg_hours"),
                func.count().filter(source.c.sla_breached_at.is_not(None)).label("breaches"),
                func.count().filter(source.c.reopened_count > 0).label("reopened"),
                func.avg(source.c.csat_score).label("csat"),
            ).select_from(source)
        )
        totals = rows.one()
        total = int(totals.total)
        statuses = {status.value: 0 for status in ComplaintStatus}
        for status, count in await db.execute(
            select(source.c.status, func.count()).group_by(source.c.status)
        ):
            statuses[str(status)] = int(count)

        day = func.date_trunc("day", func.timezone("UTC", source.c.created_at))
        daily = await db.execute(select(day, func.count()).group_by(day).order_by(day).limit(367))
        by_company = await db.execute(
            select(source.c.company_id, InsuranceCompany.name_en, func.count())
            .join(InsuranceCompany, InsuranceCompany.id == source.c.company_id)
            .group_by(source.c.company_id, InsuranceCompany.name_en)
            .order_by(func.count().desc())
            .limit(100)
        )
        by_type = await db.execute(
            select(source.c.insurance_type, func.count())
            .group_by(source.c.insurance_type)
            .order_by(func.count().desc())
        )
        by_agent = await db.execute(
            select(source.c.assigned_agent_id, StaffUser.full_name, func.count())
            .select_from(source)
            .outerjoin(StaffUser, StaffUser.id == source.c.assigned_agent_id)
            .group_by(source.c.assigned_agent_id, StaffUser.full_name)
            .order_by(func.count().desc())
            .limit(100)
        )
        bucket = case(
            (hours < 24, "under_24h"),
            (hours < 72, "24_to_72h"),
            (hours < 168, "72_to_168h"),
            else_="over_168h",
        )
        distribution = await db.execute(
            select(bucket, func.count()).where(source.c.resolved_at.is_not(None)).group_by(bucket)
        )
        cairo_time = func.timezone("Africa/Cairo", source.c.created_at)
        weekday = func.extract("dow", cairo_time)
        hour = func.extract("hour", cairo_time)
        heatmap = await db.execute(
            select(weekday, hour, func.count()).group_by(weekday, hour).order_by(weekday, hour)
        )
        # Recurring issue categories, not free-text customer descriptions/PII.
        issue = func.coalesce(Category.name_en, "Uncategorized")
        issues = await db.execute(
            select(issue, func.count())
            .select_from(source)
            .outerjoin(Category, Category.id == source.c.category_id)
            .group_by(issue)
            .order_by(func.count().desc())
            .limit(20)
        )
        scorecards = await db.execute(
            select(
                source.c.company_id,
                InsuranceCompany.name_en,
                func.count(),
                func.avg(hours).filter(source.c.resolved_at.is_not(None)),
                func.count().filter(source.c.sla_breached_at.is_not(None)),
                func.avg(source.c.csat_score),
            )
            .join(InsuranceCompany, InsuranceCompany.id == source.c.company_id)
            .group_by(source.c.company_id, InsuranceCompany.name_en)
            .order_by(func.count().desc())
            .limit(100)
        )
        return {
            "kpis": {
                "total": total,
                **statuses,
                "avg_resolution_hours": float(totals.avg_hours)
                if totals.avg_hours is not None
                else None,
                "sla_breaches": int(totals.breaches),
                "reopen_rate": round(int(totals.reopened) / total, 4) if total else 0.0,
                "csat_avg": float(totals.csat) if totals.csat is not None else None,
            },
            "time_series": [
                {"date": day.date().isoformat(), "arrived": int(n)} for day, n in daily
            ],
            "by_status": [{"status": label, "count": n} for label, n in statuses.items()],
            "by_company": [
                {"company_id": cid, "name": name, "count": int(n)} for cid, name, n in by_company
            ],
            "by_type": [{"insurance_type": str(kind), "count": int(n)} for kind, n in by_type],
            "by_agent": [
                {"agent_id": agent, "name": name, "count": int(n)} for agent, name, n in by_agent
            ],
            "resolution_hours": {label: int(n) for label, n in distribution},
            "heatmap": [
                {"weekday": int(day), "hour": int(h), "count": int(n)} for day, h, n in heatmap
            ],
            "top_issues": [{"category": name, "count": int(n)} for name, n in issues],
            "company_scorecards": [
                {
                    "company_id": cid,
                    "name": name,
                    "total": int(n),
                    "avg_resolution_hours": float(avg) if avg is not None else None,
                    "sla_breaches": int(breach),
                    "csat_avg": float(csat) if csat is not None else None,
                }
                for cid, name, n, avg, breach, csat in scorecards
            ],
        }

    async def channel_series(
        self, db: AsyncSession, scope: AccessScope, filters: ComplaintFilter
    ) -> list[tuple[Any, str, int]]:
        """Aggregate only safe daily channel counts from the caller's scoped set."""
        source = (
            ComplaintRepository()
            .base_query(scope, filters)
            .with_only_columns(Complaint.created_at, Complaint.channel, maintain_column_froms=True)
            .subquery("visible_channel_tickets")
        )
        day = func.date_trunc("day", func.timezone("UTC", source.c.created_at))
        rows = await db.execute(
            select(day, source.c.channel, func.count())
            .group_by(day, source.c.channel)
            .order_by(day, source.c.channel)
        )
        return [(day, str(channel), int(count)) for day, channel, count in rows]

    async def rollup_rows(
        self, db: AsyncSession, scope: AccessScope, filters: ComplaintFilter
    ) -> list[dict[str, Any]] | None:
        """Pre-aggregated rows for the SAME scope, or None for a live fallback.

        The day granularity cannot answer partial days, free-text search or a
        single ticket, so those requests keep using the live scoped query.
        """
        if scope.actor.scope != "staff" or not whole_utc_days(filters):
            return None
        rows = await db.execute(
            text(
                "SELECT * FROM app_security.analytics_daily_scoped("
                "CAST(:date_from AS timestamptz), CAST(:date_to AS timestamptz), "
                "CAST(:status AS text), CAST(:company AS uuid), CAST(:kind AS text), "
                "CAST(:priority AS text), CAST(:category AS uuid), "
                "CAST(:channel AS text), CAST(:agent AS uuid))"
            ),
            {
                "date_from": filters.date_from,
                "date_to": filters.date_to,
                "status": filters.status.value if filters.status else None,
                "company": filters.company_id,
                "kind": filters.insurance_type.value if filters.insurance_type else None,
                "priority": filters.priority.value if filters.priority else None,
                "category": filters.category_id,
                "channel": filters.channel.value if filters.channel else None,
                "agent": filters.assigned_agent_id,
            },
        )
        return [dict(row._mapping) for row in rows]

    async def rollup_fresh(self, db: AsyncSession) -> bool:
        value: bool | None = await db.scalar(
            text("SELECT app_security.analytics_rollup_fresh(interval '48 hours')")
        )
        return bool(value)
