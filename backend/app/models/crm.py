"""Multi-tenant Phase 1/2 entities: tenancy, Sales CRM, issuing, policy lifecycle.

Every table here carries ``tenant_id`` from day one (spec rule: every entity is
multi-tenant). The single existing brokerage deployment is the default tenant
seeded by migration 039; new tenants are ordinary rows in ``tenants``.
Status-like fields are checked TEXT in PostgreSQL (see migration 039) so a
tenant can customise pipeline stages without DDL; ``app.domain.enums`` holds
the API-level value sets.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# The brokerage that already runs on this platform (seeded by migration 039).
DEFAULT_TENANT_ID = UUID("00000000-0000-4000-8000-0000000000a1")
_TENANT_DEFAULT = text("'00000000-0000-4000-8000-0000000000a1'::uuid")


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PipelineStage(Base):
    """A tenant-customisable sales pipeline stage (spec: حالات قابلة للتخصيص)."""

    __tablename__ = "pipeline_stages"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    code: Mapped[str] = mapped_column(Text)
    name_ar: Mapped[str] = mapped_column(Text)
    name_en: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    is_won: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_lost: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_converted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    lead_no: Mapped[str] = mapped_column(Text, unique=True)
    company_name: Mapped[str] = mapped_column(Text)
    contact_name: Mapped[str | None] = mapped_column(Text)
    mobile: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, server_default="manual")
    insurance_type: Mapped[str] = mapped_column(Text, server_default="general")
    insurer_company_id: Mapped[UUID | None] = mapped_column(ForeignKey("insurance_companies.id"))
    stage_code: Mapped[str] = mapped_column(Text, server_default="new")
    assigned_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    expected_premium: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    lost_reason: Mapped[str | None] = mapped_column(Text)
    converted_corporate_client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("corporate_clients.id")
    )
    converted_at: Mapped[datetime | None]
    # Next scheduled follow-up; the Celery beat job surfaces overdue ones.
    next_followup_at: Mapped[datetime | None]
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LeadActivity(Base):
    """Calls, meetings, notes, tasks and appointments against a lead."""

    __tablename__ = "lead_activities"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    lead_id: Mapped[UUID] = mapped_column(ForeignKey("leads.id"))
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None]
    done_at: Mapped[datetime | None]
    outcome: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class IssuingChecklistTemplate(Base):
    __tablename__ = "issuing_checklist_templates"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    insurance_type: Mapped[str] = mapped_column(Text)
    document_name: Mapped[str] = mapped_column(Text)
    is_required: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class IssuingRequest(Base):
    __tablename__ = "issuing_requests"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    lead_id: Mapped[UUID | None] = mapped_column(ForeignKey("leads.id"))
    corporate_client_id: Mapped[UUID | None] = mapped_column(ForeignKey("corporate_clients.id"))
    insurer_company_id: Mapped[UUID] = mapped_column(ForeignKey("insurance_companies.id"))
    insurance_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="draft")
    assigned_staff_id: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    insurer_reference: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None]
    issued_at: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class IssuingChecklistItem(Base):
    __tablename__ = "issuing_checklist_items"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    issuing_request_id: Mapped[UUID] = mapped_column(ForeignKey("issuing_requests.id"))
    document_name: Mapped[str] = mapped_column(Text)
    is_required: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    received_at: Mapped[datetime | None]
    verified_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BrokeragePolicy(Base):
    """Policy lifecycle record (installments, renewal, ~5% commission).

    Distinct from ``policies`` (the per-employee policy identity a request
    references): this is the commercial contract with an insurer for a
    corporate client.
    """

    __tablename__ = "brokerage_policies"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    corporate_client_id: Mapped[UUID] = mapped_column(ForeignKey("corporate_clients.id"))
    insurer_company_id: Mapped[UUID] = mapped_column(ForeignKey("insurance_companies.id"))
    policy_no: Mapped[str] = mapped_column(Text)
    insurance_type: Mapped[str] = mapped_column(Text)
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    premium: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(Text, server_default="EGP")
    installment_plan: Mapped[str] = mapped_column(Text, server_default="cash")
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), server_default=text("0.05"))
    commission_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(Text, server_default="active")
    renewal_alert_sent_at: Mapped[datetime | None]
    issuing_request_id: Mapped[UUID | None] = mapped_column(ForeignKey("issuing_requests.id"))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PolicyInstallment(Base):
    __tablename__ = "policy_installments"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    policy_id: Mapped[UUID] = mapped_column(ForeignKey("brokerage_policies.id"))
    sequence_no: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    paid_at: Mapped[datetime | None]
    payment_provider: Mapped[str | None] = mapped_column(Text)
    payment_reference: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CommissionSettlement(Base):
    __tablename__ = "commission_settlements"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    policy_id: Mapped[UUID] = mapped_column(ForeignKey("brokerage_policies.id"))
    insurer_company_id: Mapped[UUID] = mapped_column(ForeignKey("insurance_companies.id"))
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    received_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    settled_at: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    installment_id: Mapped[UUID] = mapped_column(ForeignKey("policy_installments.id"))
    provider: Mapped[str] = mapped_column(Text)
    provider_reference: Mapped[str] = mapped_column(Text, unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(Text, server_default="initiated")
    raw_status: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ImportJob(Base):
    """Excel/CSV bulk-import ledger (Phase 1 adoption step)."""

    __tablename__ = "import_jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=_TENANT_DEFAULT
    )
    kind: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    corporate_client_id: Mapped[UUID | None] = mapped_column(ForeignKey("corporate_clients.id"))
    rows_total: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rows_succeeded: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rows_failed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
