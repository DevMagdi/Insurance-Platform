"""Strict request schemas for Sales CRM, issuing, policy lifecycle and imports."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.domain.enums import (
    ChecklistItemStatus,
    InstallmentPlan,
    InsuranceType,
    IssuingStatus,
    LeadActivityKind,
    LeadSource,
    PaymentProviderName,
)
from app.domain.schemas import StrictModel


class PipelineStageUpsert(StrictModel):
    id: UUID | None = None
    code: str = Field(min_length=2, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    name_ar: str = Field(min_length=1, max_length=80)
    name_en: str = Field(min_length=1, max_length=80)
    sort_order: int = Field(default=0, ge=0, le=1000)
    is_won: bool = False
    is_lost: bool = False
    is_converted: bool = False
    is_active: bool = True

    @model_validator(mode="after")
    def _one_terminal_kind(self) -> PipelineStageUpsert:
        if sum((self.is_won, self.is_lost, self.is_converted)) > 1:
            raise ValueError("A stage can be won, lost or converted — at most one")
        return self


class LeadCreate(StrictModel):
    company_name: str = Field(min_length=2, max_length=200)
    contact_name: str | None = Field(default=None, max_length=160)
    mobile: str | None = Field(default=None, pattern=r"^\+201[0-9]{9}$")
    email: str | None = Field(default=None, max_length=254)
    source: LeadSource = LeadSource.MANUAL
    insurance_type: InsuranceType = InsuranceType.GENERAL
    insurer_company_id: UUID | None = None
    stage_code: str = Field(default="new", max_length=40)
    assigned_staff_id: UUID | None = None
    expected_premium: Decimal | None = Field(default=None, ge=0, le=Decimal("999999999"))
    notes: str | None = Field(default=None, max_length=4000)
    next_followup_at: datetime | None = None

    @field_validator("next_followup_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("A timezone-aware timestamp is required")
        return value


class LeadUpdate(StrictModel):
    company_name: str | None = Field(default=None, min_length=2, max_length=200)
    contact_name: str | None = Field(default=None, max_length=160)
    mobile: str | None = Field(default=None, pattern=r"^\+201[0-9]{9}$")
    email: str | None = Field(default=None, max_length=254)
    insurance_type: InsuranceType | None = None
    insurer_company_id: UUID | None = None
    stage_code: str | None = Field(default=None, max_length=40)
    assigned_staff_id: UUID | None = None
    expected_premium: Decimal | None = Field(default=None, ge=0, le=Decimal("999999999"))
    notes: str | None = Field(default=None, max_length=4000)
    lost_reason: str | None = Field(default=None, max_length=500)
    next_followup_at: datetime | None = None

    @field_validator("next_followup_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("A timezone-aware timestamp is required")
        return value


class LeadActivityCreate(StrictModel):
    kind: LeadActivityKind
    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=4000)
    due_at: datetime | None = None
    done_at: datetime | None = None
    outcome: str | None = Field(default=None, max_length=500)

    @field_validator("due_at", "done_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("A timezone-aware timestamp is required")
        return value


class LeadConvert(StrictModel):
    """One-click Lead → client conversion; slug/name default from the lead."""

    slug: str | None = Field(default=None, max_length=80, pattern=r"^[a-z][a-z0-9-]*$")
    name_ar: str | None = Field(default=None, max_length=200)
    name_en: str | None = Field(default=None, max_length=200)


class BrokeragePolicyCreate(StrictModel):
    corporate_client_id: UUID
    insurer_company_id: UUID
    policy_no: str = Field(min_length=3, max_length=80)
    insurance_type: InsuranceType
    starts_on: date
    ends_on: date
    premium: Decimal = Field(gt=0, le=Decimal("999999999"))
    currency: str = Field(default="EGP", min_length=3, max_length=3)
    installment_plan: InstallmentPlan = InstallmentPlan.CASH
    # Brokerage commission defaults to ~5% of the premium (spec Phase 2.3).
    commission_rate: Decimal = Field(default=Decimal("0.05"), ge=0, le=Decimal("1"))
    issuing_request_id: UUID | None = None

    @model_validator(mode="after")
    def _period(self) -> BrokeragePolicyCreate:
        if self.ends_on < self.starts_on:
            raise ValueError("Policy end precedes its start")
        return self


class SettlementUpdate(StrictModel):
    received_amount: Decimal = Field(ge=0, le=Decimal("999999999"))
    notes: str | None = Field(default=None, max_length=1000)


class InstallmentPay(StrictModel):
    provider: PaymentProviderName = PaymentProviderName.MANUAL
    reference: str | None = Field(default=None, max_length=120)


class PaymentIntentCreate(StrictModel):
    provider: PaymentProviderName

    @field_validator("provider")
    @classmethod
    def _gateway_only(cls, value: PaymentProviderName) -> PaymentProviderName:
        if value is PaymentProviderName.MANUAL:
            raise ValueError("Manual payments use the pay endpoint, not an intent")
        return value


class IssuingCreate(StrictModel):
    insurer_company_id: UUID
    insurance_type: InsuranceType
    lead_id: UUID | None = None
    corporate_client_id: UUID | None = None
    assigned_staff_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=4000)


class IssuingUpdate(StrictModel):
    status: IssuingStatus | None = None
    assigned_staff_id: UUID | None = None
    insurer_reference: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=4000)


class ChecklistItemUpdate(StrictModel):
    status: ChecklistItemStatus
    notes: str | None = Field(default=None, max_length=1000)
