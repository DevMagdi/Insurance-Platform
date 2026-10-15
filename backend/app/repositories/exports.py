"""RLS-filtered job metadata; object keys are only released by download definer."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas import ExportRequest
from app.models.operations import ExportJob


class ExportRepository:
    async def request(self, db: AsyncSession, item: ExportRequest) -> UUID:
        result = await db.scalar(
            text("SELECT app_security.request_export(:kind,CAST(:filters AS jsonb))"),
            {"kind": item.kind, "filters": item.filters.model_dump_json(exclude_none=True)},
        )
        assert isinstance(result, UUID)
        return result

    async def list(self, db: AsyncSession, owner: UUID) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(
                ExportJob.id,
                ExportJob.kind,
                ExportJob.status,
                ExportJob.filters,
                ExportJob.created_at,
                ExportJob.completed_at,
                ExportJob.expires_at,
            )
            .where(ExportJob.owner_staff_id == owner)
            .order_by(ExportJob.created_at.desc(), ExportJob.id.desc())
            .limit(50)
        )
        return [dict(row._mapping) for row in rows]

    async def authorize_download(self, db: AsyncSession, job_id: UUID) -> str | None:
        result: str | None = await db.scalar(
            text("SELECT app_security.authorize_export_download(:id)"), {"id": job_id}
        )
        return result
