"""Corporate client portal: username/password sign-in and the client file.

The brokerage's customers are companies. Each contracted company gets logins
that reach exactly one thing: its own file — the contract commercials, the
renewal date, the published documents, the covered policies, and its
employees' requests grouped per employee. Nothing about another client, and
nothing about the brokerage's internal workflow, is reachable from here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Actor
from app.domain.schemas import (
    CorporateClientUpsert,
    CorporateContractUpsert,
    CorporateLoginRequest,
    CorporatePasswordChange,
    CorporateUserCreate,
)
from app.repositories.corporate import CorporateRepository
from app.security.passwords import StaffPasswords


class CorporateAuthService:
    """Sign-in for corporate principals.

    Deliberately conservative: the same Argon2id hasher, denylist and lockout
    machinery as staff sign-in, a dummy verification on unknown usernames so
    timing cannot enumerate accounts, and a forced password change on the
    first login with an issued credential.
    """

    def __init__(self, auth: Any, repository: CorporateRepository | None = None) -> None:
        self.auth = auth
        self.repository = repository or CorporateRepository()
        self.passwords: StaffPasswords = auth.passwords

    async def login(self, item: CorporateLoginRequest, ip: str, user_agent: str) -> dict[str, Any]:
        await self.auth.limits.check("corporate:login:ip", ip, 25, 900)
        await self.auth.limits.check("corporate:login:user", item.username, 8, 900)
        await self.auth.lockout.ensure_allowed("corporate_login_ip", ip)
        await self.auth.lockout.ensure_allowed("corporate_login", item.username)

        from app.db import purpose_transaction

        async with purpose_transaction(self.auth.factory, "corporate_auth") as db:
            record = await self.repository.credentials(db, item.username)
        # verify() runs against a dummy hash when the user is unknown, so an
        # invalid username costs the same time as an invalid password.
        valid = await self.passwords.verify(record.password_hash if record else None, item.password)
        if not valid or record is None or record.status != "active" or not record.client_active:
            await self.auth.lockout.failure("corporate_login", item.username)
            await self.auth.lockout.failure("corporate_login_ip", ip)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        await self.auth.lockout.success("corporate_login", item.username)

        actor = Actor(record.id, "corporate")
        tokens = await self.auth.issue_session(actor, record.token_version, ip, user_agent)
        async with purpose_transaction(self.auth.factory, "corporate_auth") as db:
            await self.repository.mark_login(db, record.id)
        return {
            "tokens": tokens,
            "must_change_password": record.must_change_password,
            "full_name": record.full_name,
            "corporate_client_id": record.corporate_client_id,
        }

    async def change_password(
        self, db: AsyncSession, actor: Actor, item: CorporatePasswordChange
    ) -> None:
        from app.db import purpose_transaction

        async with purpose_transaction(self.auth.factory, "corporate_auth") as probe:
            record = await self.repository.credentials(probe, await self._username(db, actor))
        if record is None or not await self.passwords.verify(
            record.password_hash, item.current_password
        ):
            raise HTTPException(status_code=401, detail="Current password is incorrect")
        hashed = await self.passwords.hash(item.new_password)
        if not await self.repository.change_password(db, actor.id, hashed):
            raise HTTPException(status_code=409, detail="Password could not be changed")

    async def _username(self, db: AsyncSession, actor: Actor) -> str:
        from sqlalchemy import select

        from app.models.corporate import CorporateUser

        value = await db.scalar(select(CorporateUser.username).where(CorporateUser.id == actor.id))
        if value is None:
            raise HTTPException(status_code=401, detail="Corporate account unavailable")
        return str(value)


class CorporateService:
    """Read models for the client portal and the brokerage's admin console."""

    def __init__(self, repository: CorporateRepository | None = None) -> None:
        self.repository = repository or CorporateRepository()

    async def overview(self, db: AsyncSession) -> dict[str, Any]:
        data = await self.repository.overview(db)
        if data is None:
            raise HTTPException(status_code=403, detail="Corporate account required")
        return data

    async def members(self, db: AsyncSession, search: str | None) -> dict[str, Any]:
        rows = await self.repository.members(db, search)
        return {"items": rows, "total": len(rows)}

    async def directory(self, db: AsyncSession) -> dict[str, Any]:
        rows = await self.repository.directory(db)
        return {"items": rows, "total": len(rows)}

    async def save_client(self, db: AsyncSession, item: CorporateClientUpsert) -> dict[str, Any]:
        return {"id": await self.repository.upsert_client(db, item)}

    async def save_contract(
        self, db: AsyncSession, item: CorporateContractUpsert
    ) -> dict[str, Any]:
        return {"id": await self.repository.upsert_contract(db, item)}

    async def create_user(
        self, db: AsyncSession, item: CorporateUserCreate, passwords: StaffPasswords
    ) -> dict[str, Any]:
        hashed = await passwords.hash(item.password)
        return {"id": await self.repository.create_user(db, item, hashed)}

    async def link_member(
        self, db: AsyncSession, customer_id: UUID, client_id: UUID, employee_code: str | None
    ) -> dict[str, Any]:
        if not await self.repository.link_member(db, customer_id, client_id, employee_code):
            raise HTTPException(status_code=404, detail="Customer not found")
        return {"linked": True}
