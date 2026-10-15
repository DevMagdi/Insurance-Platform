"""Catalog reads use RLS plus explicit company/type filters; writes use DB guards."""

from __future__ import annotations

import json
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, false, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.enums import OwnershipLevel
from app.models.operations import CannedReply, SlaRule
from app.models.tickets import Category

CatalogKind = Literal["company", "category", "sla_rule", "template"]
FUNCTIONS: dict[CatalogKind, str] = {
    "company": "save_company",
    "category": "save_category",
    "sla_rule": "save_sla_rule",
    "template": "save_template",
}


class CatalogRepository:
    async def save(
        self,
        db: AsyncSession,
        kind: CatalogKind,
        item_id: UUID | None,
        payload: str,
    ) -> UUID | None:
        # The only interpolated values are from the module-level, immutable whitelist.
        name = FUNCTIONS[kind]
        value: UUID | None = await db.scalar(
            text(f"SELECT app_security.{name}(:id, CAST(:data AS jsonb))"),
            {"id": item_id, "data": payload},
        )
        return value

    async def companies(self, db: AsyncSession) -> list[dict[str, Any]] | None:
        value = await db.scalar(text("SELECT app_security.admin_companies()"))
        if isinstance(value, str):
            return cast(list[dict[str, Any]], json.loads(value))
        return cast(list[dict[str, Any]] | None, value)

    @staticmethod
    def company_predicate(scope: AccessScope, company: Any) -> ColumnElement[bool]:
        if scope.insurance_types:
            # Categories/templates are shared across types, so cannot be managed
            # by an actor with only health or general insurance-type scope.
            return false()
        if scope.all_companies:
            return true() if scope.ownership == OwnershipLevel.ALL else company.is_not(None)
        return company.in_(scope.company_ids) if scope.company_ids else false()

    async def categories(self, db: AsyncSession, scope: AccessScope) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(
                Category.id,
                Category.company_id,
                Category.parent_id,
                Category.name_en,
                Category.name_ar,
                Category.is_active,
            )
            .where(self.company_predicate(scope, Category.company_id))
            .order_by(Category.name_en, Category.id)
            .limit(500)
        )
        return [dict(row._mapping) for row in rows]

    async def slas(self, db: AsyncSession, scope: AccessScope) -> list[dict[str, Any]]:
        company = SlaRule.company_id
        company_filter: ColumnElement[bool]
        if scope.all_companies:
            company_filter = (
                true()
                if scope.ownership == OwnershipLevel.ALL and not scope.insurance_types
                else company.is_not(None)
            )
        else:
            company_filter = company.in_(scope.company_ids) if scope.company_ids else false()
        kind_filter = (
            SlaRule.insurance_type.in_(scope.insurance_types) if scope.insurance_types else true()
        )
        rows = await db.execute(
            select(
                SlaRule.id,
                SlaRule.company_id,
                SlaRule.insurance_type,
                SlaRule.priority,
                SlaRule.response_minutes,
                SlaRule.resolution_minutes,
                SlaRule.is_active,
            )
            .where(company_filter, kind_filter)
            .order_by(SlaRule.created_at.desc(), SlaRule.id)
            .limit(500)
        )
        return [dict(row._mapping) for row in rows]

    async def templates(self, db: AsyncSession, scope: AccessScope) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(
                CannedReply.id,
                CannedReply.company_id,
                CannedReply.title,
                CannedReply.body_en,
                CannedReply.body_ar,
                CannedReply.is_active,
            )
            .where(self.company_predicate(scope, CannedReply.company_id))
            .order_by(CannedReply.title, CannedReply.id)
            .limit(500)
        )
        return [dict(row._mapping) for row in rows]
