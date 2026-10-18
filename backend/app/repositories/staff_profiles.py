"""Narrow DB helpers; the runtime role has no direct profile/department access."""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class StaffProfileRepository:
    async def departments(self, db: AsyncSession) -> list[dict[str, Any]] | None:
        return cast(
            list[dict[str, Any]] | None,
            json_value(await db.scalar(text("SELECT app_security.staff_departments()"))),
        )

    async def create_department(
        self, db: AsyncSession, name: str, description: str, is_active: bool
    ) -> UUID | None:
        return cast(
            UUID | None,
            await db.scalar(
                text("SELECT app_security.create_department(:name, :description, :active)"),
                {"name": name, "description": description, "active": is_active},
            ),
        )

    async def update_department(
        self, db: AsyncSession, department_id: UUID, name: str, description: str, is_active: bool
    ) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.update_department(:id, :name, :description, :active)"),
                {
                    "id": department_id,
                    "name": name,
                    "description": description,
                    "active": is_active,
                },
            )
        )

    async def assign_department(self, db: AsyncSession, user_id: UUID, department_id: UUID) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.set_staff_department(:user, :department)"),
                {"user": user_id, "department": department_id},
            )
        )

    async def profile(self, db: AsyncSession, user_id: UUID) -> dict[str, Any] | None:
        row = (
            await db.execute(
                text("SELECT * FROM app_security.staff_profile(:user)"), {"user": user_id}
            )
        ).one_or_none()
        if row is None:
            return None
        return {
            "metadata": json_value(row.metadata),
            "personal_ciphertext": row.personal_ciphertext,
        }

    async def avatar(self, db: AsyncSession, user_id: UUID) -> bytes | None:
        return cast(
            bytes | None,
            await db.scalar(text("SELECT app_security.staff_avatar(:user)"), {"user": user_id}),
        )

    async def email_connection(self, db: AsyncSession) -> dict[str, Any] | None:
        row = (
            await db.execute(text("SELECT * FROM app_security.staff_email_connection()"))
        ).one_or_none()
        return dict(row._mapping) if row else None
