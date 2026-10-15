"""Integration settings and duplicate-candidate reads; writes go through DB guards."""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.schemas import AssignmentPreview, AssignmentRuleSpec, IntegrationSpec
from app.models.operations import AssignmentRule
from app.models.tickets import DuplicateCandidate


class IntegrationRepository:
    async def save(
        self, db: AsyncSession, item_id: UUID | None, item: IntegrationSpec
    ) -> UUID | None:
        value: UUID | None = await db.scalar(
            text("SELECT app_security.save_integration(:id, CAST(:data AS jsonb))"),
            {"id": item_id, "data": item.model_dump_json(exclude_none=True)},
        )
        return value

    async def settings(self, db: AsyncSession) -> list[dict[str, Any]] | None:
        value = await db.scalar(text("SELECT app_security.admin_integrations()"))
        if isinstance(value, str):
            return cast("list[dict[str, Any]]", json.loads(value))
        return cast("list[dict[str, Any]] | None", value)

    async def delete(self, db: AsyncSession, item_id: UUID) -> bool:
        value: bool | None = await db.scalar(
            text("SELECT app_security.delete_integration(:id)"), {"id": item_id}
        )
        return bool(value)


class AssignmentRuleRepository:
    async def rules(self, db: AsyncSession, scope: AccessScope) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(
                AssignmentRule.id,
                AssignmentRule.company_id,
                AssignmentRule.insurance_type,
                AssignmentRule.category_id,
                AssignmentRule.priority,
                AssignmentRule.target_team_id,
                AssignmentRule.target_agent_id,
                AssignmentRule.strategy,
                AssignmentRule.sort_order,
                AssignmentRule.is_active,
                AssignmentRule.updated_at,
            )
            .order_by(AssignmentRule.sort_order, AssignmentRule.created_at, AssignmentRule.id)
            .limit(200)
        )
        return [dict(row._mapping) for row in rows]

    async def save(
        self, db: AsyncSession, item_id: UUID | None, item: AssignmentRuleSpec
    ) -> UUID | None:
        value: UUID | None = await db.scalar(
            text("SELECT app_security.save_assignment_rule(:id, CAST(:data AS jsonb))"),
            {"id": item_id, "data": item.model_dump_json(exclude_none=True)},
        )
        return value

    async def delete(self, db: AsyncSession, item_id: UUID) -> bool:
        value: bool | None = await db.scalar(
            text("SELECT app_security.delete_assignment_rule(:id)"), {"id": item_id}
        )
        return bool(value)

    async def preview(self, db: AsyncSession, item: AssignmentPreview) -> dict[str, Any] | None:
        value = await db.scalar(
            text(
                "SELECT app_security.preview_assignment(CAST(:company AS uuid), "
                "CAST(:kind AS insurance_type), CAST(:priority AS complaint_priority), "
                "CAST(:category AS uuid))"
            ),
            {
                "company": item.company_id,
                "kind": item.insurance_type.value,
                "priority": item.priority.value,
                "category": item.category_id,
            },
        )
        if value is None:
            return None
        return cast("dict[str, Any]", value)


class DuplicateCandidateRepository:
    async def open_candidates(
        self, db: AsyncSession, scope: AccessScope, limit: int
    ) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(
                DuplicateCandidate.id,
                DuplicateCandidate.first_complaint_id,
                DuplicateCandidate.second_complaint_id,
                DuplicateCandidate.confidence,
                DuplicateCandidate.decision,
                DuplicateCandidate.created_at,
            )
            .where(DuplicateCandidate.decision == "pending")
            .order_by(DuplicateCandidate.confidence.desc(), DuplicateCandidate.created_at.desc())
            .limit(limit)
        )
        return [dict(row._mapping) for row in rows]

    async def scan_ticket(self, db: AsyncSession, complaint_id: UUID, created_at: Any) -> int:
        value: int | None = await db.scalar(
            text("SELECT app_security.detect_duplicate_candidates(:id, :created)"),
            {"id": complaint_id, "created": created_at},
        )
        return int(value or 0)
