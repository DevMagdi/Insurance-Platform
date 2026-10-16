"""Customer self-service profile access via narrow SECURITY DEFINER helpers.

The runtime role may read only its own customers row through RLS, and all
writes are funnelled through ``app_security.update_customer_profile`` so a
customer can never edit another identity or an immutable identity field.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas import CustomerProfileUpdate


class CustomerProfileRepository:
    async def profile(self, db: AsyncSession) -> dict[str, Any] | None:
        result: dict[str, Any] | str | None = await db.scalar(
            text("SELECT app_security.customer_profile()")
        )
        if result is None:
            return None
        return json.loads(result) if isinstance(result, str) else dict(result)

    async def update(self, db: AsyncSession, item: CustomerProfileUpdate) -> dict[str, Any] | None:
        result: dict[str, Any] | str | None = await db.scalar(
            text(
                "SELECT app_security.update_customer_profile("
                ":full_name, :email, :whatsapp, CAST(:preferences AS jsonb))"
            ),
            {
                "full_name": item.full_name,
                "email": item.email,
                "whatsapp": item.whatsapp_available,
                "preferences": item.notification_preferences.model_dump_json(),
            },
        )
        if result is None:
            return None
        return json.loads(result) if isinstance(result, str) else dict(result)

    async def mark_notification_read(self, db: AsyncSession, notification_id: UUID) -> bool:
        result = await db.scalar(
            text("SELECT app_security.mark_customer_notification_read(:id)"),
            {"id": notification_id},
        )
        return bool(result)
