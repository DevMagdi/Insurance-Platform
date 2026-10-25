"""CRM / policy-lifecycle queries.

Staff company scope is applied in Python on top of PostgreSQL RLS (which only
lets the ``staff`` actor scope touch these tables at all): a staff member
scoped to specific insurers never sees other insurers' leads or policies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, false, func, or_, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.models.crm import (
    DEFAULT_TENANT_ID,
    BrokeragePolicy,
    CommissionSettlement,
    ImportJob,
    IssuingChecklistItem,
    IssuingChecklistTemplate,
    IssuingRequest,
    Lead,
    LeadActivity,
    PipelineStage,
    PolicyInstallment,
)


def _insurer_scope(scope: AccessScope, column: Any, assignee: Any = None) -> ColumnElement[bool]:
    if scope.actor.scope != "staff":
        return false()
    if scope.all_companies:
        return true()
    if not scope.company_ids:
        return false()
    scoped: ColumnElement[bool] = column.in_(scope.company_ids)
    if assignee is not None:
        # A lead with no insurer chosen yet still belongs to its assignee.
        scoped = or_(scoped, and_(column.is_(None), assignee == scope.actor.id))
    return scoped


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def lead_dict(lead: Lead) -> dict[str, Any]:
    return {
        "id": str(lead.id),
        "lead_no": lead.lead_no,
        "company_name": lead.company_name,
        "contact_name": lead.contact_name,
        "mobile": lead.mobile,
        "email": lead.email,
        "source": lead.source,
        "insurance_type": lead.insurance_type,
        "insurer_company_id": str(lead.insurer_company_id) if lead.insurer_company_id else None,
        "stage_code": lead.stage_code,
        "assigned_staff_id": str(lead.assigned_staff_id) if lead.assigned_staff_id else None,
        "expected_premium": str(lead.expected_premium)
        if lead.expected_premium is not None
        else None,
        "notes": lead.notes,
        "lost_reason": lead.lost_reason,
        "converted_corporate_client_id": (
            str(lead.converted_corporate_client_id) if lead.converted_corporate_client_id else None
        ),
        "converted_at": _iso(lead.converted_at),
        "next_followup_at": _iso(lead.next_followup_at),
        "created_at": _iso(lead.created_at),
        "updated_at": _iso(lead.updated_at),
    }


class CrmRepository:
    tenant_id = DEFAULT_TENANT_ID

    async def stages(self, db: AsyncSession) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(PipelineStage)
            .where(PipelineStage.tenant_id == self.tenant_id, PipelineStage.is_active.is_(True))
            .order_by(PipelineStage.sort_order, PipelineStage.code)
        )
        return [
            {
                "id": str(row.id),
                "code": row.code,
                "name_ar": row.name_ar,
                "name_en": row.name_en,
                "sort_order": row.sort_order,
                "is_won": row.is_won,
                "is_lost": row.is_lost,
                "is_converted": row.is_converted,
            }
            for row in rows.scalars()
        ]

    async def leads(
        self,
        db: AsyncSession,
        scope: AccessScope,
        *,
        stage: str | None = None,
        source: str | None = None,
        assigned: UUID | None = None,
        q: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        stmt = (
            select(Lead)
            .where(
                Lead.tenant_id == self.tenant_id,
                _insurer_scope(scope, Lead.insurer_company_id, Lead.assigned_staff_id),
            )
            .order_by(Lead.updated_at.desc())
            .limit(limit)
        )
        if stage:
            stmt = stmt.where(Lead.stage_code == stage)
        if source:
            stmt = stmt.where(Lead.source == source)
        if assigned:
            stmt = stmt.where(Lead.assigned_staff_id == assigned)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                or_(
                    Lead.company_name.ilike(like),
                    Lead.contact_name.ilike(like),
                    Lead.mobile.ilike(like),
                )
            )
        return [lead_dict(row) for row in (await db.execute(stmt)).scalars()]

    async def lead(self, db: AsyncSession, scope: AccessScope, lead_id: UUID) -> Lead | None:
        stmt = select(Lead).where(
            Lead.id == lead_id,
            Lead.tenant_id == self.tenant_id,
            _insurer_scope(scope, Lead.insurer_company_id, Lead.assigned_staff_id),
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def activities(self, db: AsyncSession, lead_id: UUID) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(LeadActivity)
            .where(LeadActivity.lead_id == lead_id)
            .order_by(LeadActivity.created_at.desc())
        )
        return [
            {
                "id": str(row.id),
                "kind": row.kind,
                "title": row.title,
                "body": row.body,
                "due_at": _iso(row.due_at),
                "done_at": _iso(row.done_at),
                "outcome": row.outcome,
                "created_at": _iso(row.created_at),
            }
            for row in rows.scalars()
        ]

    async def pipeline(self, db: AsyncSession, scope: AccessScope) -> dict[str, Any]:
        stages = await self.stages(db)
        counts = {
            row.stage_code: row.n
            for row in await db.execute(
                select(Lead.stage_code, func.count().label("n"))
                .where(
                    Lead.tenant_id == self.tenant_id,
                    _insurer_scope(scope, Lead.insurer_company_id, Lead.assigned_staff_id),
                )
                .group_by(Lead.stage_code)
            )
        }
        by_stage = {
            stage["code"]: {**stage, "count": counts.get(stage["code"], 0)} for stage in stages
        }
        total = sum(counts.values())
        won = sum(n for code, n in counts.items() if by_stage.get(code, {}).get("is_won"))
        lost = sum(n for code, n in counts.items() if by_stage.get(code, {}).get("is_lost"))
        converted = sum(
            n for code, n in counts.items() if by_stage.get(code, {}).get("is_converted")
        )
        closed = won + lost + converted
        return {
            "stages": list(by_stage.values()),
            "total": total,
            "won": won,
            "lost": lost,
            "converted": converted,
            "win_rate": round((won + converted) / closed * 100, 1) if closed else 0.0,
            "conversion_rate": round(converted / total * 100, 1) if total else 0.0,
        }

    async def sales_performance(self, db: AsyncSession, scope: AccessScope) -> list[dict[str, Any]]:
        stmt = text(
            """
            SELECT s.id::text AS staff_id, s.full_name,
                   count(l.id) AS leads,
                   count(*) FILTER (WHERE l.stage_code IN
                       (SELECT code FROM pipeline_stages WHERE is_won OR is_converted)) AS won,
                   count(*) FILTER (WHERE l.converted_at IS NOT NULL) AS converted,
                   COALESCE(sum(l.expected_premium), 0) AS expected_premium
            FROM leads l JOIN staff_users s ON s.id = l.assigned_staff_id
            WHERE l.tenant_id = :tenant
            GROUP BY s.id, s.full_name ORDER BY won DESC, leads DESC
            """
        )
        if not scope.all_companies:
            # Scoped staff only get their own row.
            stmt = text(stmt.text.replace("GROUP BY", "AND l.assigned_staff_id = :me GROUP BY"))
            rows = await db.execute(stmt, {"tenant": self.tenant_id, "me": scope.actor.id})
        else:
            rows = await db.execute(stmt, {"tenant": self.tenant_id})
        return [
            {**dict(row._mapping), "expected_premium": str(row.expected_premium)} for row in rows
        ]

    async def policies(
        self, db: AsyncSession, scope: AccessScope, limit: int = 200
    ) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(BrokeragePolicy)
            .where(
                BrokeragePolicy.tenant_id == self.tenant_id,
                _insurer_scope(scope, BrokeragePolicy.insurer_company_id),
            )
            .order_by(BrokeragePolicy.ends_on)
            .limit(limit)
        )
        return [self.policy_dict(row) for row in rows.scalars()]

    def policy_dict(self, policy: BrokeragePolicy) -> dict[str, Any]:
        return {
            "id": str(policy.id),
            "corporate_client_id": str(policy.corporate_client_id),
            "insurer_company_id": str(policy.insurer_company_id),
            "policy_no": policy.policy_no,
            "insurance_type": policy.insurance_type,
            "starts_on": _iso(policy.starts_on),
            "ends_on": _iso(policy.ends_on),
            "premium": str(policy.premium),
            "currency": policy.currency,
            "installment_plan": policy.installment_plan,
            "commission_rate": str(policy.commission_rate),
            "commission_amount": str(policy.commission_amount),
            "status": policy.status,
        }

    async def policy_detail(
        self, db: AsyncSession, scope: AccessScope, policy_id: UUID
    ) -> dict[str, Any] | None:
        policy = (
            await db.execute(
                select(BrokeragePolicy).where(
                    BrokeragePolicy.id == policy_id,
                    BrokeragePolicy.tenant_id == self.tenant_id,
                    _insurer_scope(scope, BrokeragePolicy.insurer_company_id),
                )
            )
        ).scalar_one_or_none()
        if policy is None:
            return None
        installments = await db.execute(
            select(PolicyInstallment)
            .where(PolicyInstallment.policy_id == policy_id)
            .order_by(PolicyInstallment.sequence_no)
        )
        settlements = await db.execute(
            select(CommissionSettlement).where(CommissionSettlement.policy_id == policy_id)
        )
        return {
            **self.policy_dict(policy),
            "installments": [
                {
                    "id": str(row.id),
                    "sequence_no": row.sequence_no,
                    "due_date": _iso(row.due_date),
                    "amount": str(row.amount),
                    "status": row.status,
                    "paid_at": _iso(row.paid_at),
                    "payment_provider": row.payment_provider,
                    "payment_reference": row.payment_reference,
                }
                for row in installments.scalars()
            ],
            "settlements": [
                {
                    "id": str(row.id),
                    "expected_amount": str(row.expected_amount),
                    "received_amount": str(row.received_amount),
                    "status": row.status,
                    "settled_at": _iso(row.settled_at),
                }
                for row in settlements.scalars()
            ],
        }

    async def renewals(
        self, db: AsyncSession, scope: AccessScope, days: int
    ) -> list[dict[str, Any]]:
        horizon = datetime.now(UTC).date() + timedelta(days=days)
        rows = await db.execute(
            select(BrokeragePolicy)
            .where(
                BrokeragePolicy.tenant_id == self.tenant_id,
                BrokeragePolicy.status == "active",
                BrokeragePolicy.ends_on <= horizon,
                _insurer_scope(scope, BrokeragePolicy.insurer_company_id),
            )
            .order_by(BrokeragePolicy.ends_on)
        )
        return [self.policy_dict(row) for row in rows.scalars()]

    async def issuing(self, db: AsyncSession, scope: AccessScope) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(IssuingRequest)
            .where(
                IssuingRequest.tenant_id == self.tenant_id,
                _insurer_scope(
                    scope, IssuingRequest.insurer_company_id, IssuingRequest.assigned_staff_id
                ),
            )
            .order_by(IssuingRequest.created_at.desc())
            .limit(200)
        )
        result = []
        for row in rows.scalars():
            result.append(await self.issuing_dict(db, row))
        return result

    async def issuing_dict(self, db: AsyncSession, row: IssuingRequest) -> dict[str, Any]:
        items = await db.execute(
            select(IssuingChecklistItem)
            .where(IssuingChecklistItem.issuing_request_id == row.id)
            .order_by(IssuingChecklistItem.created_at)
        )
        return {
            "id": str(row.id),
            "lead_id": str(row.lead_id) if row.lead_id else None,
            "corporate_client_id": str(row.corporate_client_id)
            if row.corporate_client_id
            else None,
            "insurer_company_id": str(row.insurer_company_id),
            "insurance_type": row.insurance_type,
            "status": row.status,
            "assigned_staff_id": str(row.assigned_staff_id) if row.assigned_staff_id else None,
            "insurer_reference": row.insurer_reference,
            "submitted_at": _iso(row.submitted_at),
            "issued_at": _iso(row.issued_at),
            "notes": row.notes,
            "checklist": [
                {
                    "id": str(item.id),
                    "document_name": item.document_name,
                    "is_required": item.is_required,
                    "status": item.status,
                    "received_at": _iso(item.received_at),
                }
                for item in items.scalars()
            ],
        }

    async def checklist_templates(
        self, db: AsyncSession, insurance_type: str
    ) -> list[IssuingChecklistTemplate]:
        rows = await db.execute(
            select(IssuingChecklistTemplate)
            .where(
                IssuingChecklistTemplate.tenant_id == self.tenant_id,
                IssuingChecklistTemplate.insurance_type == insurance_type,
            )
            .order_by(IssuingChecklistTemplate.sort_order)
        )
        return list(rows.scalars())

    async def imports(self, db: AsyncSession) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(ImportJob)
            .where(ImportJob.tenant_id == self.tenant_id)
            .order_by(ImportJob.created_at.desc())
            .limit(100)
        )
        return [
            {
                "id": str(row.id),
                "kind": row.kind,
                "filename": row.filename,
                "rows_total": row.rows_total,
                "rows_succeeded": row.rows_succeeded,
                "rows_failed": row.rows_failed,
                "errors": row.errors,
                "created_at": _iso(row.created_at),
            }
            for row in rows.scalars()
        ]

    async def insurer_performance(
        self, db: AsyncSession, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Per-insurer response/resolution evidence for performance complaints.

        Runs under staff RLS, so only in-scope requests are aggregated.
        """
        rows = await db.execute(
            text(
                """
                SELECT c.company_id::text AS insurer_company_id,
                       ic.name_ar AS insurer_name_ar, ic.name_en AS insurer_name_en,
                       count(*) AS requests,
                       count(*) FILTER (WHERE c.status = 'resolved') AS resolved,
                       count(*) FILTER (WHERE c.sla_breached_at IS NOT NULL) AS sla_breaches,
                       round(avg(EXTRACT(EPOCH FROM (c.resolved_at - c.created_at)) / 3600)
                             FILTER (WHERE c.resolved_at IS NOT NULL)::numeric, 2)
                           AS avg_resolution_hours
                FROM complaints c
                JOIN insurance_companies ic ON ic.id = c.company_id
                WHERE c.created_at >= :start AND c.created_at <= :end
                GROUP BY c.company_id, ic.name_ar, ic.name_en
                ORDER BY requests DESC
                """
            ),
            {"start": start, "end": end},
        )
        return [
            {
                **dict(row._mapping),
                "avg_resolution_hours": (
                    str(row.avg_resolution_hours) if row.avg_resolution_hours is not None else None
                ),
            }
            for row in rows
        ]

    async def client_folder(
        self, db: AsyncSession, corporate_client_id: UUID
    ) -> dict[str, Any] | None:
        client = await db.execute(
            text(
                "SELECT id::text, slug, name_ar, name_en, contact_name, contact_email,"
                " contact_mobile, is_active FROM corporate_clients WHERE id = :id"
            ),
            {"id": corporate_client_id},
        )
        client_row = client.first()
        if client_row is None:
            return None
        contracts = await db.execute(
            text(
                "SELECT id::text, contract_no, contract_value, currency, payment_frequency::text,"
                " starts_on, renews_on, status::text FROM corporate_contracts"
                " WHERE corporate_client_id = :id ORDER BY renews_on DESC"
            ),
            {"id": corporate_client_id},
        )
        documents = await db.execute(
            text(
                "SELECT d.id::text, d.title, d.doc_kind, d.original_filename, d.created_at"
                " FROM corporate_contract_documents d"
                " JOIN corporate_contracts c ON c.id = d.contract_id"
                " WHERE c.corporate_client_id = :id ORDER BY d.created_at DESC LIMIT 100"
            ),
            {"id": corporate_client_id},
        )
        employees = await db.execute(
            text(
                "SELECT id::text, full_name, mobile, email, employee_code FROM customers"
                " WHERE corporate_client_id = :id ORDER BY full_name LIMIT 500"
            ),
            {"id": corporate_client_id},
        )
        tickets = await db.execute(
            text(
                "SELECT status::text AS status, count(*) AS n FROM complaints"
                " WHERE corporate_client_id = :id GROUP BY status"
            ),
            {"id": corporate_client_id},
        )
        policies = await db.execute(
            select(BrokeragePolicy).where(
                BrokeragePolicy.corporate_client_id == corporate_client_id
            )
        )

        def rows_json(result: Any) -> list[dict[str, Any]]:
            return [{k: _iso(v) for k, v in dict(row._mapping).items()} for row in result]

        return {
            "client": {k: _iso(v) for k, v in dict(client_row._mapping).items()},
            "contracts": [
                {**row, "contract_value": str(row["contract_value"])}
                for row in rows_json(contracts)
            ],
            "documents": rows_json(documents),
            "employees": rows_json(employees),
            "tickets_by_status": {row.status: row.n for row in tickets},
            "policies": [self.policy_dict(row) for row in policies.scalars()],
        }

    async def document_search(self, db: AsyncSession, q: str) -> dict[str, Any]:
        """Smart search across contract documents, policies and request subjects."""
        like = f"%{q}%"
        documents = await db.execute(
            text(
                "SELECT id::text, title, doc_kind, original_filename"
                " FROM corporate_contract_documents"
                " WHERE title ILIKE :q OR original_filename ILIKE :q LIMIT 50"
            ),
            {"q": like},
        )
        policies = await db.execute(
            select(BrokeragePolicy).where(BrokeragePolicy.policy_no.ilike(like)).limit(50)
        )
        tickets = await db.execute(
            text(
                "SELECT ticket_no, subject, status::text AS status FROM complaints"
                " WHERE subject ILIKE :q ORDER BY created_at DESC LIMIT 50"
            ),
            {"q": like},
        )
        return {
            "documents": [dict(row._mapping) for row in documents],
            "policies": [self.policy_dict(row) for row in policies.scalars()],
            "tickets": [dict(row._mapping) for row in tickets],
        }
