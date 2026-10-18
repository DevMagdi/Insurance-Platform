"""Auth persistence; unauthenticated lookups use only narrow definer functions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Actor
from app.models.core import AuthSession


@dataclass(frozen=True)
class StaffCredentials:
    id: UUID
    email: str
    full_name: str
    password_hash: str | None
    status: str
    must_change_password: bool
    two_factor_enabled: bool
    two_factor_required: bool
    totp_secret_ciphertext: bytes | None
    token_version: int
    global_2fa_required: bool

    @classmethod
    def from_row(cls, row: Any) -> StaffCredentials:
        return cls(
            id=row.staff_id,
            email=row.email,
            full_name=row.full_name,
            password_hash=row.password_hash,
            status=row.status,
            must_change_password=row.must_change_password,
            two_factor_enabled=row.two_factor_enabled,
            two_factor_required=row.two_factor_required,
            totp_secret_ciphertext=row.totp_secret_ciphertext,
            token_version=row.token_version,
            global_2fa_required=row.global_2fa_required,
        )


class AuthRepository:
    async def staff_by_email(self, db: AsyncSession, email: str) -> StaffCredentials | None:
        row = (
            await db.execute(
                text("SELECT * FROM app_security.staff_credentials(:email)"), {"email": email}
            )
        ).one_or_none()
        return StaffCredentials.from_row(row) if row else None

    async def staff_by_id(self, db: AsyncSession, staff_id: UUID) -> StaffCredentials | None:
        row = (
            await db.execute(
                text("SELECT * FROM app_security.staff_credentials_by_id(:id)"), {"id": staff_id}
            )
        ).one_or_none()
        return StaffCredentials.from_row(row) if row else None

    async def customers_by_mobile(
        self, db: AsyncSession, mobile: str, blind: bytes | None
    ) -> list[tuple[UUID, int]]:
        rows = (
            await db.execute(
                text("SELECT * FROM app_security.customer_by_mobile(:mobile, :blind)"),
                {"mobile": mobile, "blind": blind},
            )
        ).all()
        return [(row.customer_id, row.token_version) for row in rows]

    async def register_customer(
        self,
        db: AsyncSession,
        *,
        full_name: str,
        mobile: str,
        ciphertext: bytes,
        blind: bytes,
        last4: str,
        email: str | None,
        whatsapp: bool,
    ) -> UUID:
        value = await db.scalar(
            text(
                "SELECT app_security.register_customer("
                ":name, :mobile, :cipher, :blind, :last4, :email, :whatsapp)"
            ),
            {
                "name": full_name,
                "mobile": mobile,
                "cipher": ciphertext,
                "blind": blind,
                "last4": last4,
                "email": email,
                "whatsapp": whatsapp,
            },
        )
        assert isinstance(value, UUID)
        return value

    async def create_session(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        sid: UUID,
        family_id: UUID,
        digest: bytes,
        version: int,
        expires_at: datetime,
        ip: str | None,
        user_agent: str | None,
        rotated_from: UUID | None = None,
    ) -> AuthSession:
        record = AuthSession(
            id=sid,
            subject_id=actor.id,
            subject_kind=actor.scope,
            family_id=family_id,
            refresh_digest=digest,
            token_version=version,
            expires_at=expires_at,
            ip_address=ip,
            user_agent=user_agent,
            rotated_from_id=rotated_from,
        )
        db.add(record)
        await db.flush()
        return record

    async def get_session(
        self, db: AsyncSession, sid: UUID, *, lock: bool = False
    ) -> AuthSession | None:
        stmt = select(AuthSession).where(AuthSession.id == sid)
        if lock:
            stmt = stmt.with_for_update()
        return (await db.scalars(stmt)).one_or_none()

    async def current_version(self, db: AsyncSession) -> int | None:
        result: int | None = await db.scalar(text("SELECT app_security.current_subject_version()"))
        return result

    async def revoke_family(self, db: AsyncSession, family_id: UUID) -> None:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.family_id == family_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def revoke_all(self, db: AsyncSession, actor: Actor) -> None:
        await db.execute(
            update(AuthSession)
            .where(
                AuthSession.subject_id == actor.id,
                AuthSession.subject_kind == actor.scope,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )

    async def list_sessions(self, db: AsyncSession, actor: Actor) -> list[AuthSession]:
        return list(
            (
                await db.scalars(
                    select(AuthSession)
                    .where(
                        AuthSession.subject_kind == actor.scope,
                        AuthSession.subject_id == actor.id,
                        AuthSession.expires_at > datetime.now(UTC),
                        AuthSession.revoked_at.is_(None),
                    )
                    .order_by(AuthSession.created_at.desc())
                    .limit(50)
                )
            ).all()
        )

    async def change_password(self, db: AsyncSession, password_hash: str) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.change_own_password(:hash)"), {"hash": password_hash}
            )
        )

    async def set_totp(self, db: AsyncSession, ciphertext: bytes) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.set_own_totp(:secret)"), {"secret": ciphertext}
            )
        )

    async def record_login(self, db: AsyncSession) -> None:
        await db.execute(text("SELECT app_security.record_staff_login()"))

    async def invite(
        self,
        db: AsyncSession,
        *,
        email: str,
        name: str,
        digest: bytes,
        expires_at: datetime,
        department_id: UUID,
    ) -> UUID:
        value = await db.scalar(
            text(
                "SELECT app_security.create_staff_invitation_in_department("
                ":email, :name, :digest, :expires, :department)"
            ),
            {
                "email": email,
                "name": name,
                "digest": digest,
                "expires": expires_at,
                "department": department_id,
            },
        )
        assert isinstance(value, UUID)
        return value

    async def accept_invite(
        self, db: AsyncSession, digest: bytes, password_hash: str
    ) -> UUID | None:
        result: UUID | None = await db.scalar(
            text("SELECT app_security.accept_staff_invitation(:digest, :password_hash)"),
            {"digest": digest, "password_hash": password_hash},
        )
        return result

    async def invitation_details(self, db: AsyncSession, digest: bytes) -> dict[str, Any] | None:
        result = await db.scalar(
            text("SELECT app_security.staff_invitation_details(:digest)"), {"digest": digest}
        )
        return json.loads(result) if isinstance(result, str) else result  # type: ignore[no-any-return]

    async def complete_invite(
        self,
        db: AsyncSession,
        *,
        digest: bytes,
        user_id: UUID,
        password_hash: str,
        name: str | None,
        personal: bytes | None,
        blind: bytes | None,
        last4: str | None,
        avatar: bytes | None,
    ) -> UUID | None:
        result: UUID | None = await db.scalar(
            text(
                "SELECT app_security.complete_staff_invitation(:digest, :user, :password, :name, "
                ":personal, :blind, :last4, :avatar)"
            ),
            {
                "digest": digest,
                "user": user_id,
                "password": password_hash,
                "name": name,
                "personal": personal,
                "blind": blind,
                "last4": last4,
                "avatar": avatar,
            },
        )
        return result

    async def reissue_invite(
        self, db: AsyncSession, user_id: UUID, digest: bytes, expires_at: datetime
    ) -> str | None:
        result: str | None = await db.scalar(
            text("SELECT app_security.reissue_staff_invitation(:user, :digest, :expires)"),
            {"user": user_id, "digest": digest, "expires": expires_at},
        )
        return result
