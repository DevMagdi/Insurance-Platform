"""Versioned scope-hash Redis cache; DB session/RLS authorization precedes cache reads."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.schemas import ComplaintFilter
from app.repositories.analytics import AnalyticsRepository


class AnalyticsService:
    def __init__(self, repository: AnalyticsRepository, redis: Redis) -> None:
        self.repository = repository
        self.redis = redis

    @staticmethod
    def key(scope: AccessScope, filters: ComplaintFilter, version: int) -> str:
        normalized = {
            "actor": str(scope.actor.id),
            "kind": scope.actor.scope,
            "version": version,
            "companies": sorted(map(str, scope.company_ids)),
            "all_companies": scope.all_companies,
            "types": sorted(kind.value for kind in scope.insurance_types),
            "ownership": scope.ownership.value if scope.ownership else None,
            "teams": sorted(map(str, scope.team_ids)),
            "filters": filters.model_dump(mode="json"),
        }
        digest = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"analytics:v1:scope:{digest}"

    async def rollup(
        self, db: AsyncSession, scope: AccessScope, filters: ComplaintFilter, version: int
    ) -> dict[str, Any]:
        """Materialized-view analytics for whole UTC days, with a live fallback.

        The rollup is an optimization, never an authority: rows are re-scoped by
        app_security.analytics_daily_scoped, and any request it cannot answer
        exactly (partial days, free-text search, stale view) falls back to the
        live scoped query so the numbers never silently omit tickets.
        """
        self._require_window(scope, filters)
        cache_key = self.key(scope, filters, version) + ":rollup"
        cached = await self.redis.get(cache_key)
        if cached is not None:
            return json.loads(cached)  # type: ignore[no-any-return]
        fresh = await self.repository.rollup_fresh(db)
        rows = await self.repository.rollup_rows(db, scope, filters) if fresh else None
        if rows is None:
            live = await self.repository.report(db, scope, filters)
            payload: dict[str, Any] = {
                "source": "live",
                "rollup_fresh": fresh,
                "kpis": live["kpis"],
                "time_series": live["time_series"],
                "by_status": live["by_status"],
                "by_company": live["by_company"],
                "by_type": live["by_type"],
                "by_agent": live["by_agent"],
            }
        else:
            payload = {"source": "rollup", "rollup_fresh": True, **self.aggregate(rows)}
        start, end = self._window_bounds(filters)
        payload["window"] = {"date_from": start.isoformat(), "date_to": end.isoformat()}
        result = jsonable_encoder(payload)
        await self.redis.set(cache_key, json.dumps(result), ex=20)
        return result  # type: ignore[no-any-return]

    @staticmethod
    def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Fold rollup rows into chart-ready series with exact sums."""
        totals = {
            "total": 0,
            "resolved": 0,
            "sla_breaches": 0,
            "response_breaches": 0,
            "reopened": 0,
            "resolution_hours_sum": 0.0,
            "csat_sum": 0.0,
            "csat_count": 0,
            "first_response_count": 0,
            "first_response_hours_sum": 0.0,
        }
        days: dict[str, int] = {}
        statuses: dict[str, int] = {}
        companies: dict[str, int] = {}
        types: dict[str, int] = {}
        agents: dict[str, int] = {}
        for row in rows:
            totals["total"] += int(row["total"])
            totals["resolved"] += int(row["resolved_count"])
            totals["sla_breaches"] += int(row["total"]) if row["sla_breached"] else 0
            totals["response_breaches"] += int(row["total"]) if row["response_breached"] else 0
            totals["reopened"] += int(row["total"]) if row["reopened"] else 0
            totals["resolution_hours_sum"] += float(row["resolution_hours_sum"])
            totals["csat_sum"] += float(row["csat_sum"])
            totals["csat_count"] += int(row["csat_count"])
            totals["first_response_count"] += int(row["first_response_count"])
            totals["first_response_hours_sum"] += float(row["first_response_hours_sum"])
            day = row["day"].isoformat() if hasattr(row["day"], "isoformat") else str(row["day"])
            days[day] = days.get(day, 0) + int(row["total"])
            status = str(row["status"])
            statuses[status] = statuses.get(status, 0) + int(row["total"])
            company = str(row["company_id"])
            companies[company] = companies.get(company, 0) + int(row["total"])
            kind = str(row["insurance_type"])
            types[kind] = types.get(kind, 0) + int(row["total"])
            agent = str(row["agent_id"])
            agents[agent] = agents.get(agent, 0) + int(row["total"])
        total = totals["total"]
        return {
            "kpis": {
                "total": total,
                "resolved": totals["resolved"],
                "sla_breaches": totals["sla_breaches"],
                "response_breaches": totals["response_breaches"],
                "reopened": totals["reopened"],
                "reopen_rate": round(totals["reopened"] / total, 4) if total else 0.0,
                "avg_resolution_hours": round(
                    totals["resolution_hours_sum"] / totals["resolved"], 2
                )
                if totals["resolved"]
                else None,
                "avg_first_response_hours": round(
                    totals["first_response_hours_sum"] / totals["first_response_count"], 2
                )
                if totals["first_response_count"]
                else None,
                "csat_avg": round(totals["csat_sum"] / totals["csat_count"], 2)
                if totals["csat_count"]
                else None,
            },
            "time_series": [{"date": day, "arrived": count} for day, count in sorted(days.items())],
            "by_status": [
                {"status": status, "count": count} for status, count in sorted(statuses.items())
            ],
            "by_company": [
                {"company_id": company, "count": count}
                for company, count in sorted(companies.items(), key=lambda item: -item[1])
            ],
            "by_type": [
                {"insurance_type": kind, "count": count}
                for kind, count in sorted(types.items(), key=lambda item: -item[1])
            ],
            "by_agent": [
                {"agent_id": agent, "count": count}
                for agent, count in sorted(agents.items(), key=lambda item: -item[1])
            ],
        }

    def _require_window(self, scope: AccessScope, filters: ComplaintFilter) -> None:
        """Both bounds are mandatory; analytics never runs on an open window."""
        self._window_bounds(filters)
        if scope.actor.scope != "staff":
            raise HTTPException(status_code=403, detail="Staff analytics only")

    @staticmethod
    def _window_bounds(filters: ComplaintFilter) -> tuple[datetime, datetime]:
        if filters.date_from is None or filters.date_to is None:
            raise HTTPException(status_code=422, detail="A start and end date are required")
        if filters.date_to - filters.date_from > timedelta(days=366):
            raise HTTPException(status_code=422, detail="Limit analytics to 366 days")
        return filters.date_from, filters.date_to

    async def overview(
        self, db: AsyncSession, scope: AccessScope, filters: ComplaintFilter, version: int
    ) -> dict[str, Any]:
        self._require_window(scope, filters)
        cache_key = self.key(scope, filters, version)
        cached = await self.redis.get(cache_key)
        if cached is not None:
            return json.loads(cached)  # type: ignore[no-any-return]
        result = jsonable_encoder(await self.repository.report(db, scope, filters))
        start, end = self._window_bounds(filters)
        result["window"] = {"date_from": start.isoformat(), "date_to": end.isoformat()}
        await self.redis.set(cache_key, json.dumps(result), ex=20)
        return result  # type: ignore[no-any-return]
