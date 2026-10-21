"""Corporate client persistence.

Every read and write goes through a SECURITY DEFINER helper in the
``app_security`` schema, so the authorisation rule lives next to the data and
is re-evaluated by PostgreSQL even if an API-layer guard is ever missed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas import (
    CorporateClientUpsert,
    CorporateContractUpsert,
    CorporateUserCreate,
)


@dataclass(frozen=True)
class CorporateCredentials:
    """What the login flow needs, and nothing that could leak elsewhere."""

    id: UUID
    corporate_client_id: UUID
    full_name: str
    password_hash: str
    status: str
    must_change_password: bool
    token_version: int
    client_active: bool


class CorporateRepository:
    async def credentials(self, db: AsyncSession, username: str) -> CorporateCredentials | None:
        row = (
            await db.execute(
                text("SELECT * FROM app_security.corporate_login_record(:username)"),
                {"username": username.lower()},
            )
        ).one_or_none()
        if row is None:
            return None
        return CorporateCredentials(
            id=row.id,
            corporate_client_id=row.corporate_client_id,
            full_name=row.full_name,
            password_hash=row.password_hash,
            status=row.status,
            must_change_password=row.must_change_password,
            token_version=row.token_version,
            client_active=row.client_active,
        )

    async def mark_login(self, db: AsyncSession, user_id: UUID) -> bool:
        return bool(
            await db.scalar(text("SELECT app_security.corporate_mark_login(:id)"), {"id": user_id})
        )

    async def token_version(self, db: AsyncSession, user_id: UUID) -> int | None:
        result: int | None = await db.scalar(
            text("SELECT app_security.corporate_token_version(:id)"), {"id": user_id}
        )
        return result

    async def change_password(self, db: AsyncSession, user_id: UUID, password_hash: str) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.corporate_change_password(:id, :hash)"),
                {"id": user_id, "hash": password_hash},
            )
        )

    async def client_id(self, db: AsyncSession) -> UUID | None:
        result: UUID | None = await db.scalar(text("SELECT app_security.corporate_client_id()"))
        return result

    # ------------------------------------------------------------------ read

    async def overview(self, db: AsyncSession) -> dict[str, Any] | None:
        result = await db.scalar(text("SELECT app_security.corporate_overview()"))
        return result if isinstance(result, dict) else None

    async def members(self, db: AsyncSession, search: str | None) -> list[dict[str, Any]]:
        result = await db.scalar(
            text("SELECT app_security.corporate_members(:search)"), {"search": search}
        )
        return list(result) if isinstance(result, list) else []

    async def directory(self, db: AsyncSession) -> list[dict[str, Any]]:
        result = await db.scalar(text("SELECT app_security.corporate_directory()"))
        return list(result) if isinstance(result, list) else []

    async def document(self, db: AsyncSession, document_id: UUID) -> Any | None:
        return (
            await db.execute(
                text("SELECT * FROM app_security.read_contract_document(:id)"),
                {"id": document_id},
            )
        ).one_or_none()

    # ----------------------------------------------------------------- write

    async def upsert_client(self, db: AsyncSession, item: CorporateClientUpsert) -> UUID:
        value: UUID | None = await db.scalar(
            text(
                "SELECT app_security.upsert_corporate_client(:id, :slug, :name_ar, :name_en,"
                " :tax_id, :contact_name, :contact_email, :contact_mobile, :address, :is_active)"
            ),
            {
                "id": item.id,
                "slug": item.slug,
                "name_ar": item.name_ar,
                "name_en": item.name_en,
                "tax_id": item.tax_id,
                "contact_name": item.contact_name,
                "contact_email": item.contact_email,
                "contact_mobile": item.contact_mobile,
                "address": item.address,
                "is_active": item.is_active,
            },
        )
        assert value is not None
        return value

    async def upsert_contract(self, db: AsyncSession, item: CorporateContractUpsert) -> UUID:
        value: UUID | None = await db.scalar(
            text(
                "SELECT app_security.upsert_corporate_contract(:id, :client, :no, :value,"
                " :currency, CAST(:frequency AS public.payment_frequency), :starts, :renews,"
                " CAST(:status AS public.contract_status), :headcount, :notes)"
            ),
            {
                "id": item.id,
                "client": item.corporate_client_id,
                "no": item.contract_no,
                "value": item.contract_value,
                "currency": item.currency,
                "frequency": item.payment_frequency.value,
                "starts": item.starts_on,
                "renews": item.renews_on,
                "status": item.status.value,
                "headcount": item.headcount,
                "notes": item.notes,
            },
        )
        assert value is not None
        return value

    async def create_user(
        self, db: AsyncSession, item: CorporateUserCreate, password_hash: str
    ) -> UUID:
        value: UUID | None = await db.scalar(
            text(
                "SELECT app_security.create_corporate_user(:client, :username, :full_name,"
                " :email, :hash)"
            ),
            {
                "client": item.corporate_client_id,
                "username": item.username,
                "full_name": item.full_name,
                "email": item.email,
                "hash": password_hash,
            },
        )
        assert value is not None
        return value

    async def link_member(
        self, db: AsyncSession, customer_id: UUID, client_id: UUID, employee_code: str | None
    ) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.link_customer_to_corporate(:customer, :client, :code)"),
                {"customer": customer_id, "client": client_id, "code": employee_code},
            )
        )

    async def store_document(
        self,
        db: AsyncSession,
        *,
        contract_id: UUID,
        title: str,
        doc_kind: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        checksum: str,
        content: bytes,
        visible: bool,
    ) -> UUID:
        value: UUID | None = await db.scalar(
            text(
                "SELECT app_security.store_contract_document(:contract, :title, :kind,"
                " :filename, :mime, :size, :checksum, :content, :visible)"
            ),
            {
                "contract": contract_id,
                "title": title,
                "kind": doc_kind,
                "filename": filename,
                "mime": mime_type,
                "size": size_bytes,
                "checksum": checksum,
                "content": content,
                "visible": visible,
            },
        )
        assert value is not None
        return value

    async def set_document_scan(self, db: AsyncSession, document_id: UUID, status: str) -> bool:
        return bool(
            await db.scalar(
                text(
                    "SELECT app_security.set_contract_document_scan(:id,"
                    " CAST(:status AS public.attachment_scan_status))"
                ),
                {"id": document_id, "status": status},
            )
        )
