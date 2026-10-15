"""Narrow SECURITY DEFINER access to integration credentials, queues and API keys."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class IntegrationRepository:
    async def save_config(
        self,
        db: AsyncSession,
        company_id: UUID | None,
        provider: str,
        ciphertext: bytes | None,
        enabled: bool,
        clear_credentials: bool = False,
    ) -> UUID:
        result = await db.scalar(
            text(
                "SELECT app_security.save_integration_config("
                ":company,:provider,:cipher,:enabled,:clear)"
            ),
            {
                "company": company_id,
                "provider": provider,
                "cipher": ciphertext,
                "enabled": enabled,
                "clear": clear_credentials,
            },
        )
        if not isinstance(result, UUID):
            raise PermissionError("Integration configuration was not authorized")
        return result

    async def list_configs(self, db: AsyncSession) -> list[dict[str, Any]]:
        result = await db.execute(text("SELECT * FROM app_security.list_integration_configs()"))
        return [dict(row._mapping) for row in result]

    async def webhook_config(
        self, db: AsyncSession, company_id: UUID, provider: str
    ) -> tuple[UUID | None, bytes] | None:
        row = (
            await db.execute(
                text("SELECT * FROM app_security.webhook_integration_config(:company,:provider)"),
                {"company": company_id, "provider": provider},
            )
        ).one_or_none()
        return (row.config_company_id, row.config_ciphertext) if row else None

    async def enqueue_inbound(
        self,
        db: AsyncSession,
        company_id: UUID,
        provider: str,
        job_kind: str,
        ciphertext: bytes,
        idempotency_digest: bytes,
        payload_digest: bytes,
    ) -> dict[str, Any]:
        row = (
            await db.execute(
                text(
                    "SELECT * FROM app_security.enqueue_inbound_job("
                    ":company,:provider,:kind,:cipher,:idem,:digest)"
                ),
                {
                    "company": company_id,
                    "provider": provider,
                    "kind": job_kind,
                    "cipher": ciphertext,
                    "idem": idempotency_digest,
                    "digest": payload_digest,
                },
            )
        ).one()
        return dict(row._mapping)

    async def enqueue_job(
        self,
        db: AsyncSession,
        *,
        job_kind: str,
        company_id: UUID,
        complaint_id: UUID,
        complaint_created_at: datetime,
        sender_staff_id: UUID,
        message_id: UUID | None,
        call_log_id: UUID | None,
        payload_ciphertext: bytes,
        idempotency_digest: bytes,
        payload_digest: bytes,
        scheduled_at: datetime | None = None,
    ) -> dict[str, Any]:
        row = (
            await db.execute(
                text(
                    "SELECT * FROM app_security.create_integration_job("
                    ":kind,:company,:complaint,:created,:sender,:message,:call,:cipher,"
                    ":idem,:digest,:scheduled)"
                ),
                {
                    "kind": job_kind,
                    "company": company_id,
                    "complaint": complaint_id,
                    "created": complaint_created_at,
                    "sender": sender_staff_id,
                    "message": message_id,
                    "call": call_log_id,
                    "cipher": payload_ciphertext,
                    "idem": idempotency_digest,
                    "digest": payload_digest,
                    "scheduled": scheduled_at,
                },
            )
        ).one()
        return dict(row._mapping)

    async def job_status(self, db: AsyncSession, job_id: UUID) -> dict[str, Any] | None:
        row = (
            await db.execute(
                text("SELECT * FROM app_security.integration_job_status(:id)"), {"id": job_id}
            )
        ).one_or_none()
        return dict(row._mapping) if row else None

    async def company_contacts(self, db: AsyncSession, company_id: UUID) -> list[str]:
        result = await db.scalar(
            text("SELECT app_security.company_contact_emails(:company)"),
            {"company": company_id},
        )
        return list(result or [])

    async def queue_call_log(
        self,
        db: AsyncSession,
        *,
        call_id: UUID,
        complaint_id: UUID,
        complaint_created_at: datetime,
        sender_staff_id: UUID,
        scheduled_at: datetime,
    ) -> UUID:
        result = await db.scalar(
            text(
                "SELECT app_security.create_call_log(:call,:complaint,:created,:sender,:scheduled)"
            ),
            {
                "call": call_id,
                "complaint": complaint_id,
                "created": complaint_created_at,
                "sender": sender_staff_id,
                "scheduled": scheduled_at,
            },
        )
        if not isinstance(result, UUID):
            raise PermissionError("Call scheduling was not authorized")
        return result

    async def set_voice_consent(
        self, db: AsyncSession, complaint_id: UUID, complaint_created_at: datetime, enabled: bool
    ) -> datetime | None:
        result = await db.scalar(
            text("SELECT app_security.set_voice_followup_consent(:id,:created,:enabled)"),
            {"id": complaint_id, "created": complaint_created_at, "enabled": enabled},
        )
        return cast(datetime | None, result)

    async def review_ai_insight(
        self,
        db: AsyncSession,
        complaint_id: UUID,
        complaint_created_at: datetime,
        insight_id: UUID,
        accept: bool,
    ) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.review_ai_insight(:complaint,:created,:insight,:accept)"),
                {
                    "complaint": complaint_id,
                    "created": complaint_created_at,
                    "insight": insight_id,
                    "accept": accept,
                },
            )
        )

    async def save_webhook(
        self,
        db: AsyncSession,
        *,
        webhook_id: UUID | None,
        company_id: UUID | None,
        url: str,
        events: list[str],
        ciphertext: bytes | None,
        active: bool,
    ) -> UUID | None:
        result = await db.scalar(
            text(
                "SELECT app_security.save_webhook(:id,:company,:url,CAST(:events AS text[]),"
                ":cipher,:active)"
            ),
            {
                "id": webhook_id,
                "company": company_id,
                "url": url,
                "events": events,
                "cipher": ciphertext,
                "active": active,
            },
        )
        return cast(UUID | None, result)

    async def list_webhooks(self, db: AsyncSession) -> list[dict[str, Any]]:
        result = await db.execute(text("SELECT * FROM app_security.list_webhooks()"))
        return [dict(row._mapping) for row in result]

    async def webhooks_for_event(
        self, db: AsyncSession, complaint_id: UUID, created_at: datetime, event: str
    ) -> list[UUID]:
        result = await db.scalars(
            text("SELECT id FROM app_security.webhooks_for_event(:complaint,:created,:event)"),
            {"complaint": complaint_id, "created": created_at, "event": event},
        )
        return list(result.all())

    async def enqueue_webhook(
        self,
        db: AsyncSession,
        *,
        webhook_id: UUID,
        complaint_id: UUID,
        created_at: datetime,
        company_id: UUID,
        event: str,
        ciphertext: bytes,
        idempotency_digest: bytes,
        payload_digest: bytes,
    ) -> UUID | None:
        result = await db.scalar(
            text(
                "SELECT app_security.enqueue_webhook_job("
                ":hook,:complaint,:created,:company,:event,:cipher,:idem,:digest)"
            ),
            {
                "hook": webhook_id,
                "complaint": complaint_id,
                "created": created_at,
                "company": company_id,
                "event": event,
                "cipher": ciphertext,
                "idem": idempotency_digest,
                "digest": payload_digest,
            },
        )
        return cast(UUID | None, result)

    async def create_api_key(
        self,
        db: AsyncSession,
        *,
        prefix: str,
        key_hash: bytes,
        permissions: list[str],
        all_companies: bool,
        company_ids: list[UUID],
        expires_at: datetime | None,
    ) -> UUID:
        result = await db.scalar(
            text(
                "SELECT app_security.create_api_key(:prefix,:hash,CAST(:permissions AS text[]),"
                ":all_companies,CAST(:companies AS uuid[]),:expires)"
            ),
            {
                "prefix": prefix,
                "hash": key_hash,
                "permissions": permissions,
                "all_companies": all_companies,
                "companies": company_ids,
                "expires": expires_at,
            },
        )
        if not isinstance(result, UUID):
            raise PermissionError("API key creation was not authorized")
        return result

    async def list_api_keys(self, db: AsyncSession) -> list[dict[str, Any]]:
        result = await db.execute(text("SELECT * FROM app_security.list_api_keys()"))
        return [dict(row._mapping) for row in result]

    async def revoke_api_key(self, db: AsyncSession, key_id: UUID) -> bool:
        return bool(
            await db.scalar(text("SELECT app_security.revoke_api_key(:id)"), {"id": key_id})
        )

    async def api_key_identity(
        self, db: AsyncSession, prefix: str, key_hash: bytes
    ) -> dict[str, Any] | None:
        row = (
            await db.execute(
                text("SELECT * FROM app_security.api_key_identity(:prefix,:hash)"),
                {"prefix": prefix, "hash": key_hash},
            )
        ).one_or_none()
        return dict(row._mapping) if row else None

    async def api_key_ticket(
        self, db: AsyncSession, prefix: str, key_hash: bytes, ticket_no: str
    ) -> dict[str, Any] | None:
        row = (
            await db.execute(
                text("SELECT * FROM app_security.api_key_ticket(:prefix,:hash,:ticket)"),
                {"prefix": prefix, "hash": key_hash, "ticket": ticket_no},
            )
        ).one_or_none()
        return dict(row._mapping) if row else None

    async def dead_letters(self, db: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
        result = await db.execute(
            text("SELECT * FROM app_security.list_integration_dead_letters(:limit)"),
            {"limit": limit},
        )
        return [dict(row._mapping) for row in result]

    async def requeue_dead_letter(self, db: AsyncSession, job_id: UUID) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.requeue_integration_job(:id)"), {"id": job_id}
            )
        )

    async def enqueue_worker_inbound(
        self,
        db: AsyncSession,
        *,
        company_id: UUID,
        job_kind: str,
        ciphertext: bytes,
        idempotency_digest: bytes,
        payload_digest: bytes,
    ) -> dict[str, Any]:
        row = (
            await db.execute(
                text(
                    "SELECT * FROM app_security.enqueue_worker_inbound("
                    ":company,:kind,:cipher,:idem,:digest)"
                ),
                {
                    "company": company_id,
                    "kind": job_kind,
                    "cipher": ciphertext,
                    "idem": idempotency_digest,
                    "digest": payload_digest,
                },
            )
        ).one()
        return dict(row._mapping)
