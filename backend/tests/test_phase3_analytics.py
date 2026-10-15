"""Date-bound analytics reuse scope predicates and cache only by versioned scope hash."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.db import Actor
from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.domain.schemas import ComplaintFilter
from app.repositories.analytics import AnalyticsRepository
from app.services.analytics import AnalyticsService


def window(**changes: object) -> ComplaintFilter:
    return ComplaintFilter.model_validate(
        {
            "date_from": "2026-09-01T00:00:00Z",
            "date_to": "2026-10-01T00:00:00Z",
            **changes,
        }
    )


async def test_report_queries_derive_every_measure_from_the_scoped_source() -> None:
    company, agent = uuid4(), uuid4()
    now = datetime(2026, 9, 1, tzinfo=UTC)
    responses = [
        SimpleNamespace(
            one=lambda: SimpleNamespace(
                total=3, avg_hours=Decimal("12.50"), breaches=1, reopened=1, csat=Decimal("4.5")
            )
        ),
        [("new", 1), ("resolved", 2)],
        [(now, 3)],
        [(company, "In-scope insurer", 3)],
        [("health", 3)],
        [(agent, 3)],
        [("under_24h", 2)],
        [(2, 9, 3)],
        [("Delayed payment", 3)],
        [(company, "In-scope insurer", 3, Decimal("12.50"), 1, Decimal("4.5"))],
    ]
    db = SimpleNamespace(execute=AsyncMock(side_effect=responses))
    scope = AccessScope(
        Actor(agent, "staff"), company_ids=frozenset({company}), ownership=OwnershipLevel.OWN
    )
    result = await AnalyticsRepository().report(db, scope, window(priority="high"))
    assert result["kpis"]["total"] == 3
    assert result["kpis"]["sla_breaches"] == 1
    assert result["kpis"]["reopen_rate"] == pytest.approx(1 / 3, abs=0.0001)
    assert result["time_series"] == [{"date": "2026-09-01", "arrived": 3}]
    assert result["heatmap"] == [{"weekday": 2, "hour": 9, "count": 3}]
    assert result["company_scorecards"][0]["name"] == "In-scope insurer"
    for call in db.execute.call_args_list:
        sql = str(call.args[0].compile(dialect=postgresql.dialect()))
        assert "visible_tickets" in sql
        assert "complaints.company_id IN" in sql
        assert "complaints.assigned_agent_id" in sql
        assert "complaints.priority" in sql
        assert "complaints.created_at" in sql
        assert "national_id" not in sql
    assert db.execute.await_count == 10


async def test_scope_cache_never_reuses_another_scope_or_version() -> None:
    cache = fakeredis.aioredis.FakeRedis()
    try:
        report = {"kpis": {"total": 1}, "company_scorecards": []}
        repo = SimpleNamespace(report=AsyncMock(return_value=report))
        service = AnalyticsService(repo, cache)
        actor = Actor(uuid4(), "staff")
        a, b = uuid4(), uuid4()
        own = AccessScope(actor, company_ids=frozenset({a}), ownership=OwnershipLevel.OWN)
        other = AccessScope(actor, company_ids=frozenset({b}), ownership=OwnershipLevel.OWN)
        filters = window(search="sensitive input")
        assert service.key(own, filters, 2) != service.key(other, filters, 2)
        assert service.key(own, filters, 2) != service.key(own, filters, 3)
        assert service.key(own, filters, 2) != service.key(own, window(search="another case"), 2)
        assert "sensitive input" not in service.key(own, filters, 2)
        first = await service.overview(SimpleNamespace(), own, filters, 2)
        assert first["kpis"]["total"] == 1
        assert (await service.overview(SimpleNamespace(), own, filters, 2)) == first
        assert repo.report.await_count == 1
        await service.overview(SimpleNamespace(), other, filters, 2)
        await service.overview(SimpleNamespace(), own, filters, 3)
        assert repo.report.await_count == 3
        for invalid in (ComplaintFilter(), window(date_to="2027-10-01T00:00:00Z")):
            with pytest.raises(HTTPException) as exc:
                await service.overview(SimpleNamespace(), own, invalid, 3)
            assert exc.value.status_code == 422
        with pytest.raises(HTTPException):
            await service.overview(
                SimpleNamespace(), AccessScope.for_customer(Actor(uuid4(), "customer")), filters, 0
            )
    finally:
        await cache.aclose()


async def test_analytics_http_requires_both_grants_and_scoped_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from httpx import ASGITransport, AsyncClient

    from app.api import analytics as analytics_api
    from app.api.deps import RequestContext, authenticated_context
    from app.main import app

    actor = Actor(uuid4(), "staff")
    scope = AccessScope(actor, company_ids=frozenset({uuid4()}), ownership=OwnershipLevel.OWN)
    ctx = RequestContext(
        SimpleNamespace(), actor, uuid4(), 1, scope, frozenset(), {}, SimpleNamespace()
    )
    repo = SimpleNamespace(
        report=AsyncMock(
            return_value={"kpis": {"total": 0}, "company_scorecards": [], "time_series": []}
        )
    )
    monkeypatch.setattr(analytics_api, "AnalyticsRepository", lambda: repo)
    cache = fakeredis.aioredis.FakeRedis()
    app.state.redis = cache

    async def override():
        yield ctx

    app.dependency_overrides[authenticated_context] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            url = (
                "/api/staff/analytics/overview?date_from=2026-09-01T00:00:00Z"
                "&date_to=2026-10-01T00:00:00Z"
            )
            assert (await client.get(url)).status_code == 403
            ctx.permissions = frozenset({"analytics.view"})
            assert (await client.get(url)).status_code == 403
            ctx.permissions = frozenset({"analytics.view", "complaints.view"})
            assert (await client.get("/api/staff/analytics/overview")).status_code == 422
            result = await client.get(url)
            assert result.status_code == 200 and result.json()["kpis"]["total"] == 0
            assert repo.report.call_args.args[1] == scope
            assert (await client.get(url.replace("overview", "scorecards"))).json() == []
            assert repo.report.await_count == 1  # Same scope/window reuses the cache.
            ctx.token_version += 1
            assert (await client.get(url)).status_code == 200
            assert repo.report.await_count == 2
    finally:
        app.dependency_overrides.clear()
        await cache.aclose()
