"""Strict request schemas for insurance operations.

Issuing workflow, brokerage policy lifecycle (installments, commission
settlements) and gateway payment intents. Sales CRM schemas moved to
``app.domain.crm_module_schemas`` with the CRM unification (041).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.domain.enums import (
    ChecklistItemStatus,
    InstallmentPlan,
    InsuranceType,
    IssuingStatus,
    PaymentProviderName,
)
from app.domain.schemas import StrictModel


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
