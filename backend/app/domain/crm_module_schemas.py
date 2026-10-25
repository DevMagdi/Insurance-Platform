"""Strict request schemas for the unified CRM module (``/api/crm``).

Pipeline stages are tenant data (`crm_pipeline_stages`), so a lead's
``stage_code`` is a bounded string validated against the tenant's active
stages in the API — not a Python enum. Fixed vocabularies (source,
priority, task type/status, activity type) stay enums in
``app.domain.enums``.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.domain.enums import (
    CrmActivityType,
    CrmLeadPriority,
    CrmLeadSource,
    CrmTaskStatus,
    CrmTaskType,
    InsuranceType,
)
from app.domain.schemas import StrictModel
from app.services.tabular import normalize_mobile

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_STAGE_CODE = r"^[a-z][a-z0-9_]*$"


def _phone(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    normalized = normalize_mobile(value)
    if normalized is None:
        raise ValueError("A valid Egyptian mobile number is required")
    return normalized


def _email(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    lowered = value.strip().lower()
    if not _EMAIL.fullmatch(lowered):
        raise ValueError("A valid email address is required")
    return lowered


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("A timezone-aware timestamp is required")
    return value


class CrmLeadCreate(StrictModel):
    full_name: str = Field(min_length=2, max_length=160)
    company_name: str | None = Field(default=None, max_length=200)
    phone: str | None = None
    email: str | None = Field(default=None, max_length=254)
    source: CrmLeadSource = CrmLeadSource.MANUAL
    stage_code: str = Field(default="new", max_length=40, pattern=_STAGE_CODE)
    priority: CrmLeadPriority = CrmLeadPriority.NORMAL
    insurance_type: InsuranceType = InsuranceType.GENERAL
    insurer_company_id: UUID | None = None
    expected_premium: Decimal | None = Field(default=None, ge=0, le=Decimal("999999999"))
    assigned_to: UUID | None = None
    notes: str | None = Field(default=None, max_length=4000)
    # Optional first follow-up; a pending task is created for it.
    next_followup_at: datetime | None = None

    _norm_phone = field_validator("phone", mode="before")(_phone)
    _norm_email = field_validator("email", mode="before")(_email)
    _aware_followup = field_validator("next_followup_at")(_aware)


class CrmLeadUpdate(StrictModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    company_name: str | None = Field(default=None, max_length=200)
    phone: str | None = None
    email: str | None = Field(default=None, max_length=254)
    source: CrmLeadSource | None = None
    stage_code: str | None = Field(default=None, max_length=40, pattern=_STAGE_CODE)
    priority: CrmLeadPriority | None = None
    insurance_type: InsuranceType | None = None
    insurer_company_id: UUID | None = None
    expected_premium: Decimal | None = Field(default=None, ge=0, le=Decimal("999999999"))
    assigned_to: UUID | None = None
    notes: str | None = Field(default=None, max_length=4000)
    lost_reason: str | None = Field(default=None, max_length=500)

    _norm_phone = field_validator("phone", mode="before")(_phone)
    _norm_email = field_validator("email", mode="before")(_email)


class CrmLeadMove(StrictModel):
    """Kanban drag-and-drop: move one lead to another pipeline stage."""

    stage_code: str = Field(max_length=40, pattern=_STAGE_CODE)
    lost_reason: str | None = Field(default=None, max_length=500)


class CrmBulkAssign(StrictModel):
    ids: list[UUID] = Field(min_length=1, max_length=500)
    assigned_to: UUID | None = None  # null unassigns


class CrmBulkStage(StrictModel):
    ids: list[UUID] = Field(min_length=1, max_length=500)
    stage_code: str = Field(max_length=40, pattern=_STAGE_CODE)


class CrmActivityCreate(StrictModel):
    type: CrmActivityType = CrmActivityType.NOTE
    description: str = Field(min_length=1, max_length=4000)


class CrmStageCreate(StrictModel):
    code: str = Field(min_length=2, max_length=40, pattern=_STAGE_CODE)
    name_ar: str = Field(min_length=1, max_length=80)
    name_en: str = Field(min_length=1, max_length=80)
    sort_order: int = Field(default=0, ge=0, le=1000)
    is_won: bool = False
    is_lost: bool = False
    is_converted: bool = False
    is_active: bool = True

    @model_validator(mode="after")
    def _one_terminal_kind(self) -> CrmStageCreate:
        if sum((self.is_won, self.is_lost, self.is_converted)) > 1:
            raise ValueError("A stage can be won, lost or converted — at most one")
        return self


class CrmStageUpdate(StrictModel):
    name_ar: str | None = Field(default=None, min_length=1, max_length=80)
    name_en: str | None = Field(default=None, min_length=1, max_length=80)
    sort_order: int | None = Field(default=None, ge=0, le=1000)
    is_won: bool | None = None
    is_lost: bool | None = None
    is_converted: bool | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _one_terminal_kind(self) -> CrmStageUpdate:
        if sum(bool(v) for v in (self.is_won, self.is_lost, self.is_converted)) > 1:
            raise ValueError("A stage can be won, lost or converted — at most one")
        return self


class CrmStageReorder(StrictModel):
    """Full ordered list of stage ids; positions become 10, 20, 30, …"""

    ids: list[UUID] = Field(min_length=1, max_length=200)


class CrmTaskCreate(StrictModel):
    type: CrmTaskType = CrmTaskType.TASK
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    due_at: datetime
    assigned_to: UUID | None = None  # defaults to the lead's assignee/actor

    _aware_due = field_validator("due_at")(_aware)


class CrmTaskUpdate(StrictModel):
    type: CrmTaskType | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    due_at: datetime | None = None
    assigned_to: UUID | None = None
    status: CrmTaskStatus | None = None

    _aware_due = field_validator("due_at")(_aware)


class CrmLeadConvert(StrictModel):
    """One-click Lead → corporate client; slug/name default from the lead."""

    slug: str | None = Field(default=None, max_length=80, pattern=r"^[a-z][a-z0-9-]*$")
    name_ar: str | None = Field(default=None, max_length=200)
    name_en: str | None = Field(default=None, max_length=200)
