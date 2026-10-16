"""Guarded staff-console metadata reads and team administration adapters."""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas import TeamSpec


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
