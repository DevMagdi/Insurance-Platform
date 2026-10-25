"""Insurance-operations entities: issuing workflow and policy lifecycle.

Every table here carries ``tenant_id`` from day one. Sales CRM used to
live in this module too; since migration 041 the *only* CRM in the
platform is the unified module in ``app.models.crm_module`` (``crm_*``
tables) — ``IssuingRequest.lead_id`` now references ``crm_leads``.
Status-like fields are checked TEXT in PostgreSQL (see migration 039);
``app.domain.enums`` holds the API-level value sets.
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
from app.models.tenancy import TENANT_DEFAULT_SQL as _TENANT_DEFAULT


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
    lead_id: Mapped[UUID | None] = mapped_column(ForeignKey("crm_leads.id"))
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
