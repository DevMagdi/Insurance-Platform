"""Database-enforced access changes and effective snapshot queries."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas import AccessSpec


class AccessRepository:
    async def snapshot(
        self, db: AsyncSession, user_id: UUID | None = None
    ) -> dict[str, Any] | None:
        result = await db.scalar(text("SELECT app_security.access_snapshot(:id)"), {"id": user_id})
        if isinstance(result, str):
            return json.loads(result)  # type: ignore[no-any-return]
        return result  # type: ignore[no-any-return]

    async def staff_directory(self, db: AsyncSession) -> list[dict[str, Any]]:
        result = await db.execute(text("SELECT * FROM app_security.staff_directory()"))
        return [dict(row._mapping) for row in result]

    async def replace(self, db: AsyncSession, target: UUID, spec: AccessSpec) -> int:
        result = await db.scalar(
            text("SELECT app_security.replace_user_access(:target, CAST(:spec AS jsonb))"),
            {"target": target, "spec": spec.model_dump_json()},
        )
        assert isinstance(result, int)
        return result

    async def deactivate(self, db: AsyncSession, target: UUID) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.deactivate_staff(:target)"), {"target": target}
            )
        )

    async def require_totp(self, db: AsyncSession, target: UUID, required: bool) -> int | None:
        result: int | None = await db.scalar(
            text("SELECT app_security.require_staff_totp(:target, :required)"),
            {"target": target, "required": required},
        )
        return result

    async def require_global_totp(self, db: AsyncSession, required: bool) -> None:
        await db.execute(
            text("SELECT app_security.require_global_totp(:required)"), {"required": required}
        )

    async def explain(self, db: AsyncSession, user_id: UUID) -> dict[str, Any] | None:
        result = await db.scalar(
            text("SELECT app_security.explain_access(:user_id)"), {"user_id": user_id}
        )
        if isinstance(result, str):
            return cast(dict[str, Any], json.loads(result))
        return cast(dict[str, Any] | None, result)

    async def roles(self, db: AsyncSession) -> list[dict[str, Any]] | None:
        result = await db.scalar(text("SELECT app_security.roles_catalog()"))
        if isinstance(result, str):
            return cast(list[dict[str, Any]], json.loads(result))
        return cast(list[dict[str, Any]] | None, result)

    async def save_role(
        self,
        db: AsyncSession,
        role_id: UUID | None,
        name: str,
        description: str,
        permissions: set[str],
    ) -> UUID:
        result = await db.scalar(
            text(
                "SELECT app_security.save_custom_role(:id, :name, :description, "
                "CAST(:permissions AS text[]))"
            ),
            {
                "id": role_id,
                "name": name,
                "description": description,
                "permissions": sorted(permissions),
            },
        )
        assert isinstance(result, UUID)
        return result

    async def clone_role(self, db: AsyncSession, source: UUID, name: str) -> UUID | None:
        result: UUID | None = await db.scalar(
            text("SELECT app_security.clone_custom_role(:source, :name)"),
            {"source": source, "name": name},
        )
        return result

    async def edit_profile(
        self,
        db: AsyncSession,
        user_id: UUID,
        email: str,
        name: str,
    ) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.edit_staff_profile(:id, :email, :name)"),
                {"id": user_id, "email": email, "name": name},
            )
        )

    async def reactivate(self, db: AsyncSession, user_id: UUID) -> bool:
        return bool(
            await db.scalar(text("SELECT app_security.reactivate_staff(:id)"), {"id": user_id})
        )

    async def force_logout(self, db: AsyncSession, user_id: UUID) -> bool:
        return bool(
            await db.scalar(text("SELECT app_security.force_staff_logout(:id)"), {"id": user_id})
        )

    async def issue_reset(
        self,
        db: AsyncSession,
        user_id: UUID,
        digest: bytes,
        expires: datetime,
    ) -> str | None:
        result: str | None = await db.scalar(
            text("SELECT app_security.issue_staff_password_reset(:id, :digest, :expires)"),
            {"id": user_id, "digest": digest, "expires": expires},
        )
        return result
