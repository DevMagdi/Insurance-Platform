"""RLS-filtered reads and guarded SECURITY DEFINER writes for privacy records."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _rows(result: Any) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in result]


class PrivacyRepository:
    async def latest_consents(self, db: AsyncSession) -> list[dict[str, Any]]:
        result = await db.execute(
            text(
                "SELECT DISTINCT ON (purpose) purpose, granted, policy_version, created_at "
                "FROM public.consent_records ORDER BY purpose, created_at DESC"
            )
        )
        return _rows(result)

    async def consent_history(self, db: AsyncSession) -> list[dict[str, Any]]:
        result = await db.execute(
            text(
                "SELECT purpose, granted, policy_version, source, created_at "
                "FROM public.consent_records ORDER BY created_at DESC LIMIT 500"
            )
        )
        return _rows(result)

    async def record_consent(
        self,
        db: AsyncSession,
        purpose: str,
        granted: bool,
        policy_version: str,
        ip: str | None,
        user_agent: str,
    ) -> UUID | None:
        value = await db.scalar(
            text(
                "SELECT app_security.record_consent("
                ":purpose, :granted, :version, CAST(:ip AS inet), :ua)"
            ),
            {
                "purpose": purpose,
                "granted": granted,
                "version": policy_version,
                "ip": ip,
                "ua": user_agent,
            },
        )
        return value if isinstance(value, UUID) else None

    async def create_request(self, db: AsyncSession, kind: str, details: str | None) -> UUID | None:
        value = await db.scalar(
            text("SELECT app_security.create_privacy_request(:kind, :details)"),
            {"kind": kind, "details": details},
        )
        return value if isinstance(value, UUID) else None

    async def requests(
        self, db: AsyncSession, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        result = await db.execute(
            text(
                "SELECT id, customer_id, kind, status, details, resolution, created_at, "
                "due_at, completed_at FROM public.privacy_requests "
                "WHERE (CAST(:status AS text) IS NULL OR status = :status) "
                "ORDER BY due_at, id LIMIT :limit"
            ),
            {"status": status, "limit": limit},
        )
        return _rows(result)

    async def decide(
        self, db: AsyncSession, request_id: UUID, status: str, resolution: str | None
    ) -> bool:
        value = await db.scalar(
            text("SELECT app_security.process_privacy_request(:id, :status, :resolution)"),
            {"id": request_id, "status": status, "resolution": resolution},
        )
        return bool(value)

    async def own_complaints(self, db: AsyncSession) -> list[dict[str, Any]]:
        """Customer data export: RLS limits complaints to the signed-in owner."""
        result = await db.execute(
            text(
                "SELECT ticket_no, created_at, insurance_type::text AS insurance_type, "
                "policy_number, subject, description, status::text AS status, "
                "resolved_at FROM public.complaints ORDER BY created_at DESC LIMIT 1000"
            )
        )
        return _rows(result)
