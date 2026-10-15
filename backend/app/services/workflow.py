"""Staff commands share a scoped transaction; any bulk failure rolls all back."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.enums import ComplaintStatus
from app.domain.schemas import AssignmentRequest, BulkAction, ComplaintFilter
from app.repositories.workflow import StaffWorkflowRepository
from app.services.complaints import ComplaintService


class StaffWorkflowService:
    def __init__(self, complaints: ComplaintService, repository: StaffWorkflowRepository) -> None:
        self.complaints = complaints
        self.repository = repository

    @staticmethod
    def require_staff(scope: AccessScope) -> None:
        if scope.actor.scope != "staff":
            raise HTTPException(status_code=403, detail="Staff access required")

    async def assign(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID, item: AssignmentRequest
    ) -> dict[str, Any]:
        self.require_staff(scope)
        parent = await self.complaints.parent(db, scope, complaint_id)
        if not await self.repository.assign(db, parent, item):
            raise HTTPException(status_code=409, detail="Assignment is not permitted")
        status = (
            ComplaintStatus.ASSIGNED
            if parent.status in (ComplaintStatus.NEW, ComplaintStatus.REOPENED)
            else parent.status
        )
        return {
            "id": complaint_id,
            "assigned_agent_id": item.agent_id,
            "team_id": item.team_id,
            "status": status,
        }

    async def escalate(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID, reason: str
    ) -> dict[str, Any]:
        self.require_staff(scope)
        parent = await self.complaints.parent(db, scope, complaint_id)
        if not await self.repository.escalate(db, parent, reason):
            raise HTTPException(status_code=409, detail="Escalation is unavailable")
        return {"id": complaint_id, "status": ComplaintStatus.TRANSFERRED, "delivery": "queued"}

    async def merge(
        self, db: AsyncSession, scope: AccessScope, source_id: UUID, target_id: UUID
    ) -> dict[str, Any]:
        self.require_staff(scope)
        if source_id == target_id:
            raise HTTPException(status_code=422, detail="Cannot merge a complaint into itself")
        source = await self.complaints.parent(db, scope, source_id)
        target = await self.complaints.parent(db, scope, target_id)
        if not await self.repository.merge(db, source, target):
            raise HTTPException(status_code=409, detail="Merge is unavailable")
        return {"id": source_id, "merged_into_id": target_id, "status": ComplaintStatus.RESOLVED}

    async def duplicates(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID
    ) -> list[dict[str, Any]]:
        self.require_staff(scope)
        parent = await self.complaints.parent(db, scope, complaint_id)
        return await self.repository.candidates(db, scope, parent)

    async def bulk(self, db: AsyncSession, scope: AccessScope, item: BulkAction) -> dict[str, Any]:
        self.require_staff(scope)
        results: list[dict[str, Any]] = []
        for complaint_id in item.complaint_ids:
            if item.action == "assign":
                assert item.assignment is not None  # Validated by BulkAction.
                results.append(await self.assign(db, scope, complaint_id, item.assignment))
            else:
                assert item.status is not None
                result = await self.complaints.change_status(db, scope, complaint_id, item.status)
                results.append({"id": complaint_id, **result})
        return {"items": results, "count": len(results)}

    async def saved_view_results(
        self, db: AsyncSession, scope: AccessScope, view_id: UUID
    ) -> dict[str, Any]:
        self.require_staff(scope)
        saved = await self.repository.saved_view(db, scope, view_id)
        if saved is None:
            raise HTTPException(status_code=404, detail="Saved view not found")
        # Saved filters never grant access. Use the caller's CURRENT scope/RLS.
        return await self.complaints.list(db, scope, ComplaintFilter.model_validate(saved.criteria))
