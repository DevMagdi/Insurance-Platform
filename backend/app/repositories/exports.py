"""RLS-filtered job metadata; artifacts are released only by the download definer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas import ExportRequest
from app.models.operations import ExportJob


@dataclass(frozen=True)
class ExportArtifact:
    """Encrypted artifact bytes plus the server-decided format."""

    content: bytes
    format: str


class ExportRepository:
    async def request(self, db: AsyncSession, item: ExportRequest) -> UUID:
        result = await db.scalar(
            text("SELECT app_security.request_export(:kind, CAST(:filters AS jsonb), :format)"),
            {
                "kind": item.kind,
                "filters": item.filters.model_dump_json(exclude_none=True),
                "format": item.format,
            },
        )
        assert isinstance(result, UUID)
        return result

    async def list(self, db: AsyncSession, owner: UUID) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(
                ExportJob.id,
                ExportJob.kind,
                ExportJob.format,
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

    async def authorize_download(self, db: AsyncSession, job_id: UUID) -> ExportArtifact | None:
        """Re-check permission, scope, expiry and audit inside one definer call."""
        row = (
            await db.execute(
                text("SELECT * FROM app_security.authorize_export_download(:id)"),
                {"id": job_id},
            )
        ).one_or_none()
        if row is None or row.content is None:
            return None
        return ExportArtifact(content=bytes(row.content), format=str(row.format))
