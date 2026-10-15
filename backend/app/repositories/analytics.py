"""Aggregates are built from the SAME AccessScope + RLS-filtered complaint set."""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.enums import ComplaintStatus
from app.domain.schemas import ComplaintFilter
from app.models.identity import InsuranceCompany
from app.models.tickets import Category, Complaint
from app.repositories.complaints import ComplaintRepository


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
            select(source.c.assigned_agent_id, func.count())
            .group_by(source.c.assigned_agent_id)
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
            "by_agent": [{"agent_id": agent, "count": int(n)} for agent, n in by_agent],
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
