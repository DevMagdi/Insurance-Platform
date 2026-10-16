"""Encrypted AI-insight persistence behind complaint scope predicates."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.models.tickets import AiInsight
from app.repositories.complaints import ComplaintRepository
from app.security.crypto import PayloadProtector
from app.services.ai import GeneratedInsight


class AiInsightRepository:
    def __init__(self, protector: PayloadProtector) -> None:
        self.protector = protector
        self.complaints = ComplaintRepository()

    def _serialize(self, item: AiInsight) -> dict[str, Any]:
        return {
            "id": item.id,
            "complaint_id": item.complaint_id,
            "kind": item.kind,
            "payload": self.protector.decrypt_json(item.payload_ciphertext),
            "confidence": float(item.confidence) if item.confidence is not None else None,
            "provider_model": item.provider_model,
            "review_status": item.review_status,
            "reviewed_by_staff_id": item.reviewed_by_staff_id,
            "created_at": item.created_at,
        }

    async def list_for_complaint(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID
    ) -> list[dict[str, Any]] | None:
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            return None
        rows = (
            await db.scalars(
                select(AiInsight)
                .where(
                    AiInsight.complaint_id == complaint.id,
                    AiInsight.complaint_created_at == complaint.created_at,
                )
                .order_by(AiInsight.created_at.desc(), AiInsight.id.desc())
                .limit(100)
            )
        ).all()
        return [self._serialize(item) for item in rows]

    async def create(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        generated: list[GeneratedInsight],
        provider_model: str,
    ) -> list[dict[str, Any]] | None:
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            return None
        created: list[AiInsight] = []
        for insight in generated:
            item = AiInsight(
                id=uuid4(),
                complaint_id=complaint.id,
                complaint_created_at=complaint.created_at,
                kind=insight.kind,
                payload_ciphertext=self.protector.encrypt_json(insight.payload),
                confidence=insight.confidence,
                provider_model=provider_model,
                review_status="suggested",
            )
            db.add(item)
            created.append(item)
            if insight.kind == "sentiment":
                sentiment = insight.payload.get("sentiment")
                if isinstance(sentiment, str):
                    complaint.sentiment = sentiment
        await db.flush()
        return [self._serialize(item) for item in created]

    async def review(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint_id: UUID,
        insight_id: UUID,
        status: str,
        reviewer_id: UUID,
    ) -> dict[str, Any] | None:
        complaint = await self.complaints.get(db, scope, complaint_id)
        if complaint is None:
            return None
        item = (
            await db.scalars(
                select(AiInsight).where(
                    AiInsight.id == insight_id,
                    AiInsight.complaint_id == complaint.id,
                    AiInsight.complaint_created_at == complaint.created_at,
                )
            )
        ).one_or_none()
        if item is None:
            return None
        item.review_status = status
        item.reviewed_by_staff_id = reviewer_id
        await db.flush()
        return self._serialize(item)
