"""Strict request schemas for the isolated CRM module (``/api/crm``)."""

from __future__ import annotations

import re
from uuid import UUID

from pydantic import Field, field_validator

from app.domain.enums import CrmActivityType, CrmLeadSource, CrmLeadStatus
from app.domain.schemas import StrictModel
from app.services.crm import normalize_mobile

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


class CrmLeadCreate(StrictModel):
    full_name: str = Field(min_length=2, max_length=160)
    phone: str | None = None
    email: str | None = Field(default=None, max_length=254)
    source: CrmLeadSource = CrmLeadSource.MANUAL
    status: CrmLeadStatus = CrmLeadStatus.NEW
    assigned_to: UUID | None = None
    notes: str | None = Field(default=None, max_length=4000)

    _norm_phone = field_validator("phone", mode="before")(_phone)
    _norm_email = field_validator("email", mode="before")(_email)


class CrmLeadUpdate(StrictModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    phone: str | None = None
    email: str | None = Field(default=None, max_length=254)
    source: CrmLeadSource | None = None
    status: CrmLeadStatus | None = None
    assigned_to: UUID | None = None
    notes: str | None = Field(default=None, max_length=4000)

    _norm_phone = field_validator("phone", mode="before")(_phone)
    _norm_email = field_validator("email", mode="before")(_email)


class CrmBulkAssign(StrictModel):
    ids: list[UUID] = Field(min_length=1, max_length=500)
    assigned_to: UUID | None = None  # null unassigns


class CrmBulkStatus(StrictModel):
    ids: list[UUID] = Field(min_length=1, max_length=500)
    status: CrmLeadStatus


class CrmActivityCreate(StrictModel):
    type: CrmActivityType = CrmActivityType.NOTE
    description: str = Field(min_length=1, max_length=4000)
