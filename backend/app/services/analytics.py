"""Versioned scope-hash Redis cache; DB session/RLS authorization precedes cache reads."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
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

    async def overview(
        self, db: AsyncSession, scope: AccessScope, filters: ComplaintFilter, version: int
    ) -> dict[str, Any]:
        if scope.actor.scope != "staff":
            raise HTTPException(status_code=403, detail="Staff analytics only")
        if filters.date_from is None or filters.date_to is None:
            raise HTTPException(status_code=422, detail="A start and end date are required")
        if filters.date_to - filters.date_from > timedelta(days=366):
            raise HTTPException(status_code=422, detail="Limit analytics to 366 days")
        cache_key = self.key(scope, filters, version)
        cached = await self.redis.get(cache_key)
        if cached is not None:
            return json.loads(cached)  # type: ignore[no-any-return]
        result = jsonable_encoder(await self.repository.report(db, scope, filters))
        result["window"] = {
            "date_from": filters.date_from.isoformat(),
            "date_to": filters.date_to.isoformat(),
        }
        await self.redis.set(cache_key, json.dumps(result), ex=20)
        return result  # type: ignore[no-any-return]
