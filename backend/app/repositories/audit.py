"""Audit timeline deliberately exposes metadata, not ticket/customer payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tickets import AuditLog


class AuditRepository:
    async def events(
        self,
        db: AsyncSession,
        start: datetime,
        end: datetime,
        limit: int,
        offset: int = 0,
        *,
        actor_id: UUID | None = None,
        complaint_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        stmt = (
            select(
                AuditLog.id,
                AuditLog.created_at,
                AuditLog.actor_staff_id,
                AuditLog.action,
                AuditLog.resource_type,
                AuditLog.resource_id,
                AuditLog.complaint_id,
            )
            .where(AuditLog.created_at >= start, AuditLog.created_at <= end)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if actor_id:
            stmt = stmt.where(AuditLog.actor_staff_id == actor_id)
        if complaint_id:
            stmt = stmt.where(AuditLog.complaint_id == complaint_id)
        return [dict(row._mapping) for row in await db.execute(stmt)]

    async def access_changes(
        self,
        db: AsyncSession,
        target_id: UUID | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        stmt = text("""
            SELECT id, actor_staff_id, target_user_id, change_type,
                   before_state, after_state, created_at
            FROM access_change_log
            WHERE (CAST(:target AS uuid) IS NULL OR target_user_id = :target)
            ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset
        """)
        return [
            dict(row._mapping)
            for row in await db.execute(
                stmt,
                {
                    "target": target_id,
                    "limit": limit,
                    "offset": offset,
                },
            )
        ]
